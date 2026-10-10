# Happ subscriptions

Run the shared action from a node checkout:

```sh
./commons/apex utils/generate-happ-subscriptions /private/subscriptions.json /private/output
```

The action builds the bundled Docker image and generates one file per user. Keep
input credentials and output subscription links outside Git. Regenerate into a
private staging directory and verify the files before replacing served outputs.

The input contains `routing`, `templates` and `users`. Templates provide `scheme`,
`host`, `port`, URI `params`, and display metadata (`country`, `nodes`). Each user's
`configs` entries select a `template` and merge user-specific values such as `id`
and `params`; `psub` selects the output filename. URI parameters are preserved.

Labels use the country, transport, node chain and user name. `NL` renders as
`🇳🇱 Netherlands`. Schemes `hysteria2` and `hy2` display `[hysteria]`; other schemes
use `params.type`, defaulting to `tcp`. This changes display text only.
