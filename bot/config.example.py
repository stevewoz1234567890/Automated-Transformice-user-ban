"""
Copy this file to ``config.py`` and fill in your accounts.

Each row is one game client: run Transformice through tfm-proxy-loader on ``proxy_port``.
Assign a **unique** ``bind_ip`` in Proxifier (or your VPN) per process so no IP is reused.

Passwords are not used by the proxy bot (you log in in the game); they are optional metadata.
"""

# Example: 11 slots — adjust count to match how many clients you run.
ACCOUNTS = [
    {"label": "1", "proxy_port": 11801, "bind_ip": "10.0.0.1"},
    {"label": "2", "proxy_port": 11802, "bind_ip": "10.0.0.2"},
    {"label": "3", "proxy_port": 11803, "bind_ip": "10.0.0.3"},
    {"label": "4", "proxy_port": 11804, "bind_ip": "10.0.0.4"},
    {"label": "5", "proxy_port": 11805, "bind_ip": "10.0.0.5"},
    {"label": "6", "proxy_port": 11806, "bind_ip": "10.0.0.6"},
    {"label": "7", "proxy_port": 11807, "bind_ip": "10.0.0.7"},
    {"label": "8", "proxy_port": 11808, "bind_ip": "10.0.0.8"},
    {"label": "9", "proxy_port": 11809, "bind_ip": "10.0.0.9"},
    {"label": "10", "proxy_port": 11810, "bind_ip": "10.0.0.10"},
    {"label": "11", "proxy_port": 11811, "bind_ip": "10.0.0.11"},
]

# Random delay between each account’s /ban (seconds), inclusive.
BAN_DELAY_MIN_SEC = 1.0
BAN_DELAY_MAX_SEC = 2.0

# Small gap between sending /room on each connected client (avoid bursting).
ROOM_STAGGER_SEC = 0.15
