# Security / audit notes — v1.0

Threat model: an authenticated ChatGPT client that has obtained access to this private tunnel can ask for screen contents, local files or commands. The agent provides meaningful OS access: granting shell means granting arbitrary actions as the logged-in user.

Security controls implemented:
- stdio MCP server only, no open TCP listening port or auto-start service.
- local opt-in policy; default deny; capability flags re-read before EVERY operation.
- file read/write paths confined to explicitly granted roots; realpath resolution blocks ../ and symlink exits.
- no overwrite by default; upper size limits for text, shell output, binaries, and screenshots.
- Android ADB requires explicit serial pairing; default disabled.
- no credentials in GitHub; optional Azure Blob metadata logs and project artifacts using Entra ID rather than storage keys.
- Azure read/write tools check the same local policy. Long-running jobs check revocation and can be cancelled remotely.
- Python subprocess invoked with shell=False; the command itself is deliberately interpreted by PowerShell/CMD when authorized.
- local emergency stop: set policy.enabled=false and stop tunnel-client. Revoke tunnel credential in Platform if compromised.

Known risks and limitations:
- While shell is enabled, commands can defeat most other user-level restrictions. Use a Windows standard user and a dedicated machine/profile if risk matters.
- One-shot screen capture may expose personal info; do not leave the tunnel attached while sensitive apps are in use.
- Lock screen, UAC secure desktop and elevated windows may be inaccessible from the non-elevated interactive process.
- Run commands may spawn child processes that survive a timeout; review complex scripts before launching.
- ChatGPT-side approvals and plan restrictions cannot be overridden by a local policy or by user instructions in a conversation.
- Local policy and credential environment are not stored in Azure; that is unavoidable runtime state. Do not put secrets in cloud logs.
- Azure audit is disabled unless CDC_AZURE_BLOB_URL is configured. When enabled, a failure writing an audit record prevents file writes and shell/Android writes, but UI actions are best-effort and are not transactionally atomic.
- No cryptographic code-signing or penetration test was performed; test on your own machine before granting broad access.
- Public repository means code can be read by others. Never add tokens, local policy.json, PII, screenshots or credentials to Git history.
