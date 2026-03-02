# Arasan Gas Middleware - Automation Scripts

## Overview

This folder contains automation scripts for running the Arasan Gas middleware on Windows.

## Quick Start

### Option 1: Manual Service Management (Recommended for Testing)

**Start all services:**
```cmd
automation\start_all_services.bat
```

This will start two background services:
- Invoice fetch (every 30 seconds)
- Sync to Catalytics (every 30 seconds)

**Stop all services:**
```cmd
automation\stop_all_services.bat
```

### Option 2: Windows Task Scheduler (Recommended for Production)

**Install scheduled tasks:**
```cmd
REM Run as Administrator
automation\install_task_scheduler.bat
```

This creates 5 scheduled tasks:
- `Arasan_Gas_Fetch_Master_Data` - Every 10 minutes
- `Arasan_Gas_Fetch_Invoices_1` - Every minute at :00
- `Arasan_Gas_Fetch_Invoices_2` - Every minute at :30
- `Arasan_Gas_Sync_To_Catalytics_1` - Every minute at :00
- `Arasan_Gas_Sync_To_Catalytics_2` - Every minute at :30

**Uninstall scheduled tasks:**
```cmd
REM Run as Administrator
automation\uninstall_task_scheduler.bat
```

## Available Scripts

### Core Automation Scripts

| Script | Purpose | Frequency |
|--------|---------|-----------|
| `fetch_master_data.bat` | Fetch customers & products from Tally | Every 10 min |
| `fetch_invoices.bat` | Fetch invoices from Tally | Every 30 sec |
| `sync_to_catalytics.bat` | Sync data to Catalytics backend | Every 30 sec |

### Service Management

| Script | Purpose |
|--------|---------|
| `start_all_services.bat` | Start continuous fetch & sync services |
| `stop_all_services.bat` | Stop all running services |
| `continuous_fetch_invoices.bat` | Continuous invoice fetch loop |
| `continuous_sync.bat` | Continuous sync loop |

### Task Scheduler Management

| Script | Purpose |
|--------|---------|
| `install_task_scheduler.bat` | Install Windows scheduled tasks (Run as Admin) |
| `uninstall_task_scheduler.bat` | Remove Windows scheduled tasks (Run as Admin) |

### Monitoring & Testing

| Script | Purpose |
|--------|---------|
| `start_dashboard.bat` | Start web dashboard UI (http://localhost:5000) |
| `monitor_status.bat` | Live status monitor (refreshes every 30 sec) |
| `view_logs.bat` | Interactive log viewer |
| `test_all_components.bat` | Test all components |

## Usage Examples

### Daily Operations

**Web Dashboard (Recommended):**
```cmd
automation\start_dashboard.bat
# Open browser to http://localhost:5000
```

**Command-Line Status:**
```cmd
automation\monitor_status.bat
```

**View logs:**
```cmd
automation\view_logs.bat
```

**Manual test:**
```cmd
automation\test_all_components.bat
```

### Troubleshooting

**If services stop working:**
```cmd
# 1. Check status
automation\monitor_status.bat

# 2. View error logs
automation\view_logs.bat
# Select option 5 to search for errors

# 3. Test components
automation\test_all_components.bat

# 4. Restart services
automation\stop_all_services.bat
automation\start_all_services.bat
```

**If Tally connection fails:**
- Ensure Tally is running
- Check Tally is on port 9000
- Verify company names in .env

**If Catalytics sync fails:**
- Ensure Django backend is running
- Check localhost:8000 is accessible
- Verify entity_id is correct in .env

## Log Files

All logs are stored in `logs/` directory:

| Log File | Content |
|----------|---------|
| `arasan_gas.log` | Main application log (fetch & sync) |
| `fetch_master_data.log` | Customer & product fetch logs |
| `fetch_invoices.log` | Invoice fetch logs |
| `sync_to_catalytics.log` | Sync operation logs |

## Recommended Setup

### For Production Environment:

1. **Install Task Scheduler tasks** (Option 2)
   - More reliable than continuous scripts
   - Runs even if not logged in
   - Automatic restart on failure

2. **Configure Windows Startup**
   - Optionally set services to start on system boot
   - Ensure Tally and Django backend start first

3. **Monitor Regularly**
   - Run `monitor_status.bat` daily
   - Check logs weekly for errors

### For Development/Testing:

1. **Use manual service management** (Option 1)
   - Easier to start/stop for testing
   - See output in real-time

2. **Run test script** before deployment
   ```cmd
   automation\test_all_components.bat
   ```

## Scheduling Details

### Master Data (Customers & Products)
- **Frequency:** Every 10 minutes
- **Reason:** Master data changes infrequently
- **Task:** `Arasan_Gas_Fetch_Master_Data`

### Invoices
- **Frequency:** Every 30 seconds
- **Reason:** Real-time invoice capture needed
- **Tasks:** `Arasan_Gas_Fetch_Invoices_1`, `Arasan_Gas_Fetch_Invoices_2`
- **Implementation:** 2 tasks offset by 30 seconds to achieve 30-second intervals

### Sync to Catalytics
- **Frequency:** Every 30 seconds
- **Reason:** Keep backend data current
- **Tasks:** `Arasan_Gas_Sync_To_Catalytics_1`, `Arasan_Gas_Sync_To_Catalytics_2`
- **Implementation:** 2 tasks offset by 30 seconds to achieve 30-second intervals

## Support

**View this README:**
```cmd
type automation\README.md | more
```

**For issues:**
1. Check logs using `view_logs.bat`
2. Run component test: `test_all_components.bat`
3. Review main documentation: `..\README.md`

## Notes

- All batch files use relative paths and work from any location
- Task Scheduler requires Administrator privileges to install/uninstall
- Continuous service scripts run in minimized windows
- 30-second intervals implemented using 2 offset tasks (Windows limitation)
