# Jev production gateway — `execution.allow_installed_gateway`

Bounded approval for the operating installed 9Router. The dispatcher's
default posture is unchanged: every loopback URL on the installed service
port **20128** is refused as "never a wrapper/gateway boundary". This flag
is the single, narrow, explicit operator opt-in that lets the **SUBMIT data
plane** — and nothing else — target the installed gateway.

## Field

```yaml
execution:
  mode: gateway
  base_url: http://127.0.0.1:20128          # literal — see limits below
  control_base_url: http://127.0.0.1:9010   # distinct wrapper control base
  credential_file: /path/to/data.cred       # submit-plane bearer
  control_credential_file: /path/to/control.cred
  allow_installed_gateway: true             # strict boolean, default false
```

`ExecutionTarget.allow_installed_gateway` is a pydantic `StrictBool`
defaulting to `false`. It lives only in the trusted central policy — a
card's `TaskSpec` (`extra="forbid"`) can never grant it, and there is no
global flag, environment override, or subclass path around it.

## Limits (validated at policy load)

When `allow_installed_gateway` is true, ALL of the following must hold:

- `mode: gateway` — a `direct` target can never opt in;
- `base_url == "http://127.0.0.1:20128"` **literally** — no `localhost`/
  `::1` alias, no path prefix, no other host or port, and the existing
  no-credentials/no-query/no-fragment hygiene still applies;
- `control_base_url` is required, is a distinct **explicit-http loopback**
  base (`127.0.0.1` / `localhost` / `::1`) and is **not** port 20128 —
  run control must use the separate wrapper base and its own credential.

Anything else — a truthy string (`"yes"`, `"true"`, `1`), a missing or
equal control base, a control base on 20128 under any spelling, a
non-loopback host on either side — is a `ValidationError` before any
client exists.

## Propagation

`dispatch_once` → `WrapperClient(base_url, control_base_url=…,
control_credential_file=…, allow_installed_gateway=policy.execution.
allow_installed_gateway)`. Inside the client the flag reaches only the
submit `_LoopbackBase` (`allow_installed_port`, honoured solely as a
literal `True`); the control base is always constructed with the default
refusal, and the client refuses the opt-in outright when no distinct
`control_base_url` is given. `control._control_client` never receives the
flag; `compiler.apply_plan` still refuses `production` targets, port 20128
and occupied catalogs regardless of the execution opt-in.

## Tested scope

`packages/kanban/tests/test_production_gateway.py` — no test contacts the
real installed service. The seam is
`wrapper_client._bounded_request` (the single socket choke point),
replaced by a recorder so the real validation/header/credential path
runs network-free:

- policy: default false, approved shape, plus the negative matrix
  (direct mode, missing/equal/20128/non-loopback/https control,
  alias/prefixed/other-port/non-loopback data base, truthy non-bools);
- boundary: default 20128 refusal on every loopback spelling, control base
  refusal, opt-in requiring literal `True` and an explicit control base;
- planes: submit POSTs `127.0.0.1:20128` with the data credential while
  `get_run`/`cancel` use the control port with the control credential;
- real dispatch construction: `dispatch_once` builds the opted submit
  client plus a flag-free control client; without the flag the same
  policy fails closed before any store/kernel/HTTP work;
- end-to-end (requires the installed Hermes venv): a real kernel card
  dispatch submits `jev.worker.code.standard` to `127.0.0.1:20128`
  through the stubbed socket and lands in review.

Untouched on purpose: the two earlier nonblocking review suggestions,
compiler management semantics, and every default boundary outside this
opt-in.
