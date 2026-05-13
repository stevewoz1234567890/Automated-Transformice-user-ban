# [Diagnosis] Flash Error #2048 vs `upstream-tcp-all-ports-failed`

> **For maintainers:** Paste this into **GitHub → Issues → New issue**, or run  
> `gh auth login` then:  
> `gh issue create --title "Diagnosis: Flash #2048 and upstream-tcp-all-ports-failed" --body-file docs/github-issue-flash-2048-upstream-tcp.md`

## Summary

Two distinct failure modes observed in logs:

| Symptom | Layer | Meaning |
|--------|--------|--------|
| **Flash Error #2048** (`securityError`) | Flash (loader SWF from `file://`) | Local-with-filesystem SWF attempts a **disallowed** socket/load to **public IP:port** (usually embedded upstream literal not fully neutralized). |
| **`upstream-tcp-all-ports-failed`** | Python / caseus proxy | **`asyncio.open_connection`** to the real game host on every configured port fails (typically **`TimeoutError`**): no TCP path from this process. |

They often appear together but are **different mechanisms**.

---

## 1. Flash Error #2048

### Cause (sandbox)

Flash treats a SWF loaded from **`file://`** as **local-with-filesystem**. That context blocks many direct uses of **remote** hosts. If bytecode still targets the real game IPv4 (e.g. `51.38.60.113:11801`), Flash throws **#2048** (“cannot load data from …”).

### Mitigation in this repo

- `bot/tfm_swf_port_patch.py` patches the ZWS loader: `localhost:11801` → local proxy, policy URL, and **`neutralize_upstream_ip_literals`** — replace plaintext upstream quad with same-length `127.0.0.1` + ASCII padding so Flash does not open `file:// → bare game IP`.
- Neutralization only replaces occurrences where the IP is followed by `:port`, NUL, or a small set of URL-like delimiters (see `neutralize_upstream_ip_literals`). **Other encodings** can leave literals in the binary → #2048 persists.
- `build_patched_loader_swf` warns if the LZMA body **still contains** the plaintext upstream after patch (“neutralize may have missed fragmented literals”).
- **`ChangeSatelliteServerPacket`** is rewritten in `BanBotProxy._proxy_satellite_server` so Flash gets **127.0.0.1** + local sat port — that addresses **post-handshake** satellite redirects, not necessarily **early loader** `/reset()` paths driven by SWF literals.

### Likely root causes when #2048 remains

1. Missed literal patterns (IP not followed by allowed tail bytes).
2. Stale `tmp/loader_patch` cache or wrong `upstream_for_neutralization` vs bytes in SWF.
3. Loader build changed; patcher needs extension for new literal layout.

### Checks

- Grep `log.txt` for: `Patched loader LZMA body still contains plaintext upstream`
- Purge patched loaders if needed (`purge_all_patched_loader_swfs` / env `BOT_PURGE_LEGACY_LOADER_PATCH_CACHE` — see `tfm_swf_port_patch.py`).

---

## 2. `upstream-tcp-all-ports-failed`

### Cause

`BanBotProxy.open_streams` sweeps all upstream ports with timeout; if every attempt fails, it logs **`upstream-tcp-all-ports-failed`** and raises `ValueError("Unable to connect to address ...")`.

### Source binding

`upstream_local_bind_tuple` in `bot/upstream_socket_bind.py` only sets `local_addr` when:

- `BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET=true` (and row `bind_ip` is a valid IPv4), or  
- `BOT_UPSTREAM_LOCAL_BIND_IPV4` is set.

**Default:** row `bind_ip` is **reference only** (Proxifier / docs); Python uses the **OS default route** unless those flags are on.

### Likely root causes for all-port timeouts

1. No route / firewall / VPN / tether handoff to game host:ports.
2. **Proxifier (or split VPN) not applied to the `python.exe` actually running the bot** — traffic never egresses the intended WAN.
3. `bind_ip` is for multi-WAN but socket bind is off: enable **`BOT_UPSTREAM_USE_ACCOUNT_BIND_IP_FOR_SOCKET`** only if that IPv4 exists **on a local NIC**; otherwise fix Proxifier rules for `python.exe`.

---

## 3. References (code)

- `bot/tfm_swf_port_patch.py` — ZWS patch, `neutralize_upstream_ip_literals`, `build_patched_loader_swf`
- `bot/ban_proxy.py` — `open_streams`, `_proxy_satellite_server`, upstream failure log block
- `bot/upstream_socket_bind.py` — `upstream_local_bind_tuple`, `account_bind_ip_for_socket_enabled`
- `bot/net_preflight.py` — related probe / bind_ip guidance

---

## Environment note

Observed on Windows with `file://` patched loader under `tmp/loader_patch/`, headless / validate path using Flash projector + caseus proxy.
