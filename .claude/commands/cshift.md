---
description: Switch or manage Claude accounts (switch, add, remove)
argument-hint: [account-number | add | add-token <token> | remove <num|email> | list]
allowed-tools: Bash
---

Run the appropriate command based on the argument "$ARGUMENTS":

| Argument | Command |
|----------|---------|
| (empty/blank) | `cshift --switch` |
| a number (1, 2, …) | `cshift --switch --account $ARGUMENTS` |
| `add` | `cshift --add-account` |
| `add-token <token>` | `cshift --add-token <token>` |
| `remove <num\|email>` | `cshift --remove-account <num\|email>` |
| `list` | `cshift --list` |

After any command that modifies or switches accounts (switch, add, remove), bust the HUD cache immediately:

```bash
cshift-hud --bust
```

Report the result in one short sentence. If it fails, show the error briefly.
