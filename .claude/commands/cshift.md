---
description: Switch or manage Claude accounts (switch, add, remove)
argument-hint: [account-number | add | add-token <token> | remove <num|email> | list]
allowed-tools: Bash
---

Run the appropriate cswap command based on the argument "$ARGUMENTS":

| Argument | Command |
|----------|---------|
| (empty/blank) | `cswap --switch` |
| a number (1, 2, …) | `cswap --switch-to $ARGUMENTS` |
| `add` | `cswap --add-account` |
| `add-token <token>` | `cswap --add-token <token>` |
| `remove <num\|email>` | `cswap --remove-account <num\|email>` |
| `list` | `cswap --list` |

After any command that modifies or switches accounts (switch, add, remove), bust the HUD cache immediately:

```bash
cshift-hud --bust
```

Report the result in one short sentence. If it fails, show the error briefly.
