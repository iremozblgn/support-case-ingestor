
# Support Portal Scheduler Deployment

This deployment keeps the existing GraphQL-first, browser-fallback scan flow, but runs it as a scheduled batch job instead of a continuous monitor.

## Install And Test

1. Create or refresh the launchd runtime copy:

```bash
cd /Users/irem/Desktop/support_portal_ingestor
bash deployment/install_runtime.sh
```

2. Activate venv:

```bash
cd "/Users/irem/Library/Application Support/Support PortalIngestor"
source .venv/bin/activate
```

3. Make script executable:

```bash
chmod +x run_support_portal_once.sh
```

4. Test manually:

```bash
./run_support_portal_once.sh
```

5. Validate the runtime layout:

```bash
bash deployment/validate_runtime.sh
```

6. Install launchd plist:

```bash
cp deployment/com.irem.support_portal.ingestor.plist ~/Library/LaunchAgents/com.irem.support_portal.ingestor.plist
```

7. Reload launchd:

```bash
launchctl unload ~/Library/LaunchAgents/com.irem.support_portal.ingestor.plist 2>/dev/null || true
launchctl load ~/Library/LaunchAgents/com.irem.support_portal.ingestor.plist
```

8. Trigger manually:

```bash
launchctl start com.irem.support_portal.ingestor
```

9. Watch logs:

```bash
tail -f "/Users/irem/Library/Application Support/Support PortalIngestor/logs/support_portal_scheduler.log"
tail -f "/Users/irem/Library/Application Support/Support PortalIngestor/logs/launchd.err.log"
```

10. Uninstall:

```bash
launchctl unload ~/Library/LaunchAgents/com.irem.support_portal.ingestor.plist
rm ~/Library/LaunchAgents/com.irem.support_portal.ingestor.plist
```

11. MFA note:

If Okta MFA/session expires, the scheduled run should fail gracefully and log `operator_action_required=true`.

The operator should run:

```bash
python scripts/test_support_portal_auto_login.py
```

Then approve MFA in Chrome for Testing.

After the session is refreshed, scheduled runs should work again.

## Troubleshooting

If `launchd.err.log` shows `Operation not permitted` while trying to execute from `Desktop`, the runtime copy was not installed correctly. Re-run `bash deployment/install_runtime.sh` and keep the launchd runtime under `~/Library/Application Support/Support PortalIngestor`.

## Default Schedule

The provided `launchd` schedule runs at:

- `09:00`
- `11:00`
- `15:00`

Add an extra `StartCalendarInterval` entry if you also want `18:00`.
