"""Target-only decoding; importing the CLI never requires a YAML dependency."""
from .contract import ContractError, normalize_user_data


def decode_user_data(content):
    import yaml

    class Loader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            if self.check_event(yaml.AliasEvent):
                raise ContractError('YAML aliases are not supported')
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            result = {}
            for key_node, value_node in node.value:
                key = self.construct_object(key_node, deep=deep)
                if not isinstance(key, str) or key in result:
                    raise ContractError('YAML mapping keys must be unique strings')
                result[key] = self.construct_object(value_node, deep=deep)
            return result

    if len(content) > 1024 * 1024:
        raise ContractError('user-data exceeds the supported size')
    try:
        document = yaml.load(content, Loader=Loader)
    except (yaml.YAMLError, UnicodeError, RecursionError):
        raise ContractError('invalid or unsupported YAML user-data') from None
    return normalize_user_data(document)
