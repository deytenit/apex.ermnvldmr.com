#!/usr/bin/env bash
# Only the parser runtime may be installed before semantic input validation.
set -euo pipefail
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
[[ $(id -u) == 0 ]] || { echo 'APEX seed requires root.' >&2; exit 1; }
[[ -r /etc/os-release ]] || { echo 'APEX seed requires Debian 13 amd64.' >&2; exit 1; }
. /etc/os-release
[[ ${ID:-} == debian && ${VERSION_ID:-} == 13 && $(dpkg --print-architecture) == amd64 ]] || {
    echo 'APEX seed requires Debian 13 amd64.' >&2; exit 1;
}

policy=/usr/sbin/policy-rc.d
state=/usr/sbin/.apex-policy-rc.d
for path in /var /var/lib /var/lib/apex-init; do
    [[ ! -L $path ]] || { echo 'Refusing redirected initialization state.' >&2; exit 1; }
done
mkdir -p -m 0700 /var/lib/apex-init
[[ $(stat -c %u /var/lib/apex-init) == 0 ]] || {
    echo 'Initialization state must be root-owned.' >&2; exit 1;
}
(( (8#$(stat -c %a /var/lib/apex-init) & 022) == 0 )) || {
    echo 'Initialization state must not be writable by other users.' >&2; exit 1;
}
[[ ! -L /var/lib/apex-init/init.lock ]] || { echo 'Refusing redirected initialization lock.' >&2; exit 1; }
exec 8>/var/lib/apex-init/init.lock
flock -n 8 || { echo 'Another APEX initialization is running.' >&2; exit 1; }
mkdir -p /run/lock
exec 9>/run/lock/apex-package-policy.lock
flock -n 9 || { echo 'Another APEX package operation is running.' >&2; exit 1; }
restore_policy() {
    [[ -d $state ]] || return 0
    [[ ! -L $state && $(stat -c %u "$state") == 0 ]] || {
        echo 'Refusing untrusted service policy recovery state.' >&2; return 1;
    }
    if [[ -e $state/ready && ( -e $policy || -L $policy ) ]]; then
        if ! cmp -s "$policy" <(printf '#!/bin/sh\nexit 101\n') &&
           ! cmp -s "$policy" "$state/original"; then
            echo 'Service policy changed outside APEX; reconcile it explicitly.' >&2
            return 1
        fi
    fi
    if [[ -e $state/original || -L $state/original ]]; then
        rm -f -- "$state/restoring"
        cp -a -- "$state/original" "$state/restoring"
        mv -Tf -- "$state/restoring" "$policy"
    elif [[ -e $state/absent ]]; then
        rm -f -- "$policy"
    elif [[ -e $state/ready ]]; then
        echo 'APEX service policy recovery is incomplete; inspect /usr/sbin/.apex-policy-rc.d.' >&2
        return 1
    fi
    rm -rf -- "$state"
}

# Release the shared host lock on exit; the target runner reacquires it later.
restore_policy
missing=()
for package in python3 python3-yaml; do
    status=$(dpkg-query -W -f='${Status}' "$package" 2>/dev/null || true)
    [[ $status == *' ok installed' ]] || missing+=("$package")
done
if (( ${#missing[@]} )); then
    mkdir -m 0700 -- "$state"
    trap restore_policy EXIT
    trap 'exit 130' INT
    trap 'exit 143' TERM
    if [[ -e $policy || -L $policy ]]; then
        cp -a -- "$policy" "$state/original"
    else
        touch "$state/absent"
    fi
    touch "$state/ready"
    printf '#!/bin/sh\nexit 101\n' > "$state/guard"
    chmod 0755 "$state/guard"
    mv -Tf -- "$state/guard" "$policy"
    apt=(apt-get -o DPkg::Lock::Timeout=60 -o Acquire::Retries=2
         -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30)
    timeout 300 "${apt[@]}" update --error-on=any
    install=("${apt[@]}" install -y --no-install-recommends --no-upgrade --no-remove "${missing[@]}")
    plan=$(timeout 300 "${install[@]}" --simulate)
    if grep -Eq '^Remv |^Inst [^ ]+ \[' <<< "$plan"; then
        echo 'Parser installation requires a package upgrade/removal; reconcile it explicitly.' >&2
        exit 1
    fi
    "${install[@]}"
fi
python3 -c 'import yaml'
