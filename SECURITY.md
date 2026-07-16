# Security Policy

## Reporting a vulnerability

Please report suspected vulnerabilities **privately** — open a
[GitHub Security Advisory](https://docs.github.com/en/code-security/security-advisories)
on this repository rather than a public issue. We aim to acknowledge reports within a
few days.

## Threat model & hardening notes

SparkReel runs **fully locally with zero cloud credentials** by default. Two optional
features widen the attack surface — understand them before exposing the app:

### The Team Console & SparkAgent (`sparkreel console`)
- The console is gated by a single shared password (`SPARKREEL_CONSOLE_PASSWORD`). It is
  **a guardrail, not a sandbox.** When the AI dev-agent is enabled
  (`SPARKREEL_AGENT_ENABLED=1`), anyone with the password can read/write files in the
  project and **run shell commands on the host**.
- No default password ships in the source. If `SPARKREEL_CONSOLE_PASSWORD` is unset, a
  random one-time password is generated at startup and printed once. **Always set a
  strong, fixed password** for any shared or internet-reachable deployment.
- Keep the console on a **trusted network**. Do not expose ports 9998/9999 to the public
  internet without a strong password (and ideally a reverse proxy / VPN in front).
- The command denylist blocks obviously destructive shell commands but does **not**
  sandbox the process and does **not** restrict cloud CLIs (e.g. `aws`). Treat console
  access as host access.

### AWS / Amazon Bedrock
- Prefer an **EC2 instance IAM role** over long-lived access keys.
- Grant **least privilege.** If you only use Bedrock, scope the role to
  `bedrock:InvokeModel` on the specific model(s) — not broad administrator access.
  A broad role combined with an enabled console agent means console access ≈ that role.

## Secrets hygiene
- **Never commit** credentials. `.env`, `.env.*`, `*.pem`, `*.key`, and `*credentials*`
  are gitignored. Use `.env.example` as a template and keep your real `.env` local.
- API keys and AWS credentials are read only from the environment / `~/.aws` / instance
  role — none are stored in the repository.
