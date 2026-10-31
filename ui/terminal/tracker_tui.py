import curses
import datetime
import json
import os
import re
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RECALC_QUEUE_PATH = REPO_ROOT / ".recalc_queue"

DEFAULT_CONFIG = {
    "columns": [
        {"key": "date", "label": "Date", "width": 12, "visible": True},
        {"key": "company", "label": "Company", "width": 20, "visible": True},
        {"key": "status", "label": "Status", "width": 12, "visible": True},
        {"key": "fit_rating", "label": "Fit", "width": 5, "visible": True},
        {"key": "role", "label": "Role", "width": 0, "visible": True}
    ],
    "theme": "dark",
    "split_mode": True
}

def get_config_path():
    xdg_config = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config:
        base = Path(xdg_config)
    else:
        base = Path.home() / ".config"
    
    app_config_dir = base / "job-tracker"
    config_file = app_config_dir / "config.json"
    
    try:
        app_config_dir.mkdir(parents=True, exist_ok=True)
        return config_file
    except Exception:
        local_cfg = Path(__file__).parent / "tui_config.json"
        if local_cfg.exists():
            return local_cfg
        return Path(__file__).parent / "config.json"

def load_config():
    path = get_config_path()
    if path.exists():
        try:
            with open(path, 'r') as f:
                config = json.load(f)
                # Ensure all default keys exist
                for key, val in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = val
                return config
        except Exception:
            pass

    # Fallback to local tui_config.json
    local_cfg = Path(__file__).parent / "tui_config.json"
    if local_cfg.exists():
        try:
            with open(local_cfg, 'r') as f:
                config = json.load(f)
                for key, val in DEFAULT_CONFIG.items():
                    if key not in config:
                        config[key] = val
                return config
        except Exception:
            pass

    return DEFAULT_CONFIG.copy()

def save_config(config):
    path = get_config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, 'w') as f:
            json.dump(config, f, indent=2)
    except Exception:
        pass

def _load_reader():
    """Import the shared store reader without making the scripts tree a package."""
    scripts = REPO_ROOT / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import reader  # noqa: PLC0415 - path is configured just above
    return reader

DATA_META = {"source": "unknown"}

def write_recalc_request(row):
    """Ask for a fit recalculation without performing one.

    The TUI cannot compute a score: that comes from the evaluation rubric. So it
    queues a structured request keyed on the application id, which `/apply` or
    an agent resolves with `tracker.py score`. Writing the whole legacy row (as
    the old queue did) meant the consumer had to re-derive company, role, and id
    from free text, and it could not tell which generation of the shape it got.
    """
    request = {
        "schemaVersion": 4,
        "kind": "fit-recalculation",
        "requestedAt": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
        "applicationId": row.get("id"),
        "company": row.get("company"),
        "role": row.get("role"),
        "currentFitRating": row.get("fit_rating") or None,
    }
    try:
        with open(RECALC_QUEUE_PATH, "w") as handle:
            json.dump(request, handle, indent=2)
    except OSError:
        pass
    return request

def load_data():
    """Rows come from the tracker store, then the cache, then the CSV.

    The legacy row shape is preserved by `scripts/reader.py`, so every renderer
    below is unchanged. `DATA_META` records which source answered, and the
    header shows it: a stale snapshot must never look live.
    """
    global DATA_META
    try:
        module = _load_reader()
        rows, meta = module.rows()
    except Exception as error:  # noqa: BLE001 - a read failure must not crash curses
        DATA_META = {"source": "unavailable", "label": "unavailable", "error": str(error)}
        return []
    DATA_META = {**meta, "label": module.badge(meta)}
    return rows

def get_column_widths(rows, cols_config):
    widths = {}
    for col in cols_config:
        key = col["key"]
        label = col["label"]
        if key == "role":
            widths[key] = 0 # Flex
            continue
        # Minimum width to accommodate label + sort indicator [N] Label v
        max_w = len(label) + 6
        for row in rows:
            val = str(row.get(key, "") or "")
            if key == "fit_rating" and not val:
                val = "?? RECALC"
            max_w = max(max_w, len(val))
        widths[key] = max_w
    return widths

def get_archive_dir(row):
    base = "documents/applications"
    if not os.path.exists(base):
        candidate = REPO_ROOT / "documents" / "applications"
        if candidate.exists():
            base = str(candidate)
    if os.path.exists(base):
        c_raw = row.get('company', '').strip()
        c_norm = re.sub(r'[^a-zA-Z0-9]', '', c_raw).lower()
        for drop in ['inc', 'llc', 'ltd', 'as', 'corp', 'corporation', 'group']:
            if c_norm.endswith(drop):
                c_norm = c_norm[:-len(drop)]
                
        matching_comp = None
        for c in os.listdir(base):
            cn = re.sub(r'[^a-zA-Z0-9]', '', c).lower()
            if cn == c_norm or c_norm in cn or cn in c_norm:
                matching_comp = c
                break
        if matching_comp:
            comp_path = os.path.join(base, matching_comp)
            roles = [r for r in os.listdir(comp_path) if not r.startswith('.')]
            if roles:
                role_raw = row.get('role', '').strip()
                r_norm = re.sub(r'[^a-zA-Z0-9]', '', role_raw).lower() if role_raw else 'general'
                matching_role = None
                for r in roles:
                    rn = re.sub(r'[^a-zA-Z0-9]', '', r).lower()
                    if rn == r_norm or r_norm in rn or rn in r_norm:
                        matching_role = r
                        break
                if not matching_role:
                    matching_role = roles[0]
                role_path = os.path.join(comp_path, matching_role)
                dates = [d for d in os.listdir(role_path) if not d.startswith('.')]
                if dates:
                    date_raw = row.get('date', '').strip()
                    if date_raw in dates:
                        return os.path.join(role_path, date_raw)
                    return os.path.join(role_path, dates[0])
                return role_path
            return comp_path

    company = row.get('company', '').lower().strip()
    for drop in ['inc.', 'inc', 'llc', 'ltd', 'a/s', 'corp', 'corporation', 'group']:
        company = company.replace(f' {drop}', '')
    company = company.replace(' ', '_').replace('.', '').replace(',', '')
    role = row.get('role', '').lower().replace(' ', '_').replace('.', '').replace(',', '').replace('(', '').replace(')', '')
    return f"documents/applications/{company}_{role}"

def load_detail_lines(row, max_width):
    raw_entries = [] # List of (text, file_path)
    
    raw_entries.append((f"COMPANY: {row.get('company')}", None))
    raw_entries.append((f"ROLE:    {row.get('role')}", None))
    raw_entries.append((f"DATE:    {row.get('date')}", None))
    raw_entries.append((f"STATUS:  {row.get('status')} (Fit: {row.get('fit_rating')})", None))
    raw_entries.append((f"NOTES:   {row.get('notes')}", None))
    raw_entries.append((f"URL:     {row.get('source')}", None))
    raw_entries.append(("", None))
    
    # Files section
    raw_entries.append(("FILES (Enter to View):", None))
    archive_dir = get_archive_dir(row)
    
    # Collect all potential files
    candidate_files = []
    if os.path.exists(archive_dir):
        for f in os.listdir(archive_dir):
            if os.path.isfile(os.path.join(archive_dir, f)):
                candidate_files.append(os.path.join(archive_dir, f))
    
    cv_file = row.get('cv_file')
    if cv_file and os.path.exists(cv_file): candidate_files.append(cv_file)
    cl_file = row.get('cover_letter_file')
    if cl_file and os.path.exists(cl_file): candidate_files.append(cl_file)
    
    # De-duplicate
    seen = set()
    unique_files = []
    for f in candidate_files:
        if f not in seen:
            unique_files.append(f)
            seen.add(f)
            
    for fpath in unique_files:
        raw_entries.append((f"  [FILE] {os.path.basename(fpath)}", fpath))
    
    raw_entries.append(("", None))

    # Outreach section
    try:
        import urllib.parse
        proto_path = ".claude/skills/job-application-assistant/outreach_protocols.json"
        if not os.path.exists(proto_path):
            candidate = REPO_ROOT / ".claude" / "skills" / "job-application-assistant" / "outreach_protocols.json"
            if candidate.exists():
                proto_path = str(candidate)
        with open(proto_path, "r") as f:
            protocols = json.load(f)

        comp = row.get('company', 'Company')
        role = row.get('role', 'Role')
        r_url = protocols.get("search_urls", {}).get("recruiter", "").replace("{company}", urllib.parse.quote(comp))
        p_url = protocols.get("search_urls", {}).get("peer", "").replace("{company}", urllib.parse.quote(comp)).replace("{role_keyword}", urllib.parse.quote(role))

        raw_entries.append(("--- OUTREACH PROTOCOLS ---", None))
        raw_entries.append((f"  [LINK] Recruiter Search", r_url))
        raw_entries.append((f"  [LINK] Peer Search", p_url))
        raw_entries.append(("", None))

        raw_entries.append(("  RECRUITER MESSAGE TEMPLATE:", None))
        r_tpl = protocols.get("templates", {}).get("recruiter", "").replace("{role}", role)
        for line in r_tpl.splitlines():
            raw_entries.append((f"  {line}", None))

        raw_entries.append(("", None))

        raw_entries.append(("  PEER MESSAGE TEMPLATE:", None))
        p_tpl = protocols.get("templates", {}).get("peer", "").replace("{role}", role)
        for line in p_tpl.splitlines():
            raw_entries.append((f"  {line}", None))

        raw_entries.append(("", None))
    except Exception as e:
        raw_entries.append((f"--- Could not load outreach protocols: {e} ---", None))
        raw_entries.append(("", None))

    if not os.path.exists(archive_dir):
        raw_entries.append((f"--- No Archive Folder Found at {archive_dir} ---", None))
    else:
        # Also include outcome.md content at the bottom for quick look
        fpath = os.path.join(archive_dir, 'outcome.md')
        if os.path.exists(fpath):
            raw_entries.append((f" {'='*15} outcome.md {'='*15} ", None))
            try:
                with open(fpath, 'r') as f:
                    for line in f:
                        raw_entries.append((line.rstrip(), None))
            except Exception as e:
                raw_entries.append((f"Error: {e}", None))
                
    wrapped_lines = []
    for text, fpath in raw_entries:
        text = text.replace('\t', '    ')
        if text.strip() == "":
            wrapped_lines.append(("", fpath))
        else:
            wrapped = textwrap.wrap(text, width=max_width - 2)
            for w_line in wrapped:
                wrapped_lines.append((w_line, fpath))
            
    return wrapped_lines

def safe_addstr(stdscr, y, x, text, attr=0):
    """Safely add a string to the screen, ignoring bottom-right corner errors."""
    try:
        stdscr.addstr(y, x, text, attr)
    except curses.error:
        pass

def init_colors(is_light_theme, no_color):
    if no_color:
        curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_WHITE) 
        curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE) 
        curses.init_pair(3, curses.COLOR_WHITE, curses.COLOR_BLACK) 
        curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK) 
    else:
        if is_light_theme:
            curses.init_pair(1, curses.COLOR_WHITE, curses.COLOR_BLUE)  
            curses.init_pair(2, curses.COLOR_WHITE, curses.COLOR_BLACK) 
            curses.init_pair(3, curses.COLOR_BLUE, curses.COLOR_WHITE)  
            curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE) 
        else:
            curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)  
            curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE) 
            curses.init_pair(3, curses.COLOR_CYAN, curses.COLOR_BLACK)  
            curses.init_pair(4, curses.COLOR_WHITE, curses.COLOR_BLACK) 

def main(stdscr):
    curses.curs_set(0)
    stdscr.nodelay(False)
    
    no_color = os.environ.get("NO_COLOR", "").strip() != ""
    if curses.has_colors() and not no_color:
        curses.start_color()
    
    config = load_config()
    is_light_theme = config.get("theme") == "light"
    init_colors(is_light_theme, no_color)
    stdscr.bkgd(' ', curses.color_pair(4))
    
    rows = load_data()
    cols_config = config["columns"]
    col_widths = get_column_widths(rows, cols_config)
    
    sort_idx = 0
    sort_dirs = {c["key"]: (c["key"] == 'date' or c["key"] == 'fit_rating') for c in cols_config}
    
    def sort_data():
        visible_cols = [c for c in cols_config if c.get("visible", True)]
        if not visible_cols: return
        col = visible_cols[sort_idx % len(visible_cols)]["key"]
        desc = sort_dirs.get(col, False)
        def sort_key(x):
            val = x.get(col, '') or ""
            if col == 'fit_rating':
                try: return int(val)
                except: return -1 if desc else 1000000
            if not val: return "" if not desc else "zzzzzz"
            return val.lower()
        rows.sort(key=sort_key, reverse=desc)
    
    sort_data()
    
    current_row = 0
    list_scroll = 0
    
    in_detail = False
    in_settings = False
    in_file_view = False
    settings_idx = 0
    
    active_selection_idx = None
    focused_pane = "LIST" # "LIST" or "DETAILS"
    detail_lines = []
    detail_scroll = 0
    detail_idx = 0
    
    file_view_lines = []
    file_view_scroll = 0
    file_view_title = ""

    while True:
        stdscr.clear()
        h, w = stdscr.getmaxyx()
        
        c_header = curses.color_pair(1) | curses.A_BOLD if curses.has_colors() else curses.A_REVERSE
        c_selected = curses.color_pair(2) if curses.has_colors() else curses.A_REVERSE
        c_highlight = curses.color_pair(3) | curses.A_BOLD if curses.has_colors() else curses.A_BOLD
        c_footer = curses.color_pair(1) if curses.has_colors() else curses.A_REVERSE
        c_default = curses.color_pair(4) if curses.has_colors() else 0

        if in_file_view:
            # --- FILE VIEW LAYER ---
            header_text = f" VIEWING FILE | {file_view_title} "
            safe_addstr(stdscr, 0, 0, header_text.ljust(w)[:w-1], c_header)
            
            max_disp = h - 3
            for i in range(max_disp):
                idx = file_view_scroll + i
                if idx >= len(file_view_lines): break
                safe_addstr(stdscr, 2 + i, 1, file_view_lines[idx][:w-2], c_default)
            
            progress = int((file_view_scroll / max(1, len(file_view_lines) - max_disp)) * 100) if len(file_view_lines) > max_disp else 100
            footer_text = f" [j/k]: Scroll | [q, ESC]: Back | {progress}% "
            safe_addstr(stdscr, h - 1, 0, footer_text.ljust(w)[:w-1], c_footer)

        elif in_settings:
            # --- SETTINGS VIEW ---
            header_text = " TUI SETTINGS | [Space]: Toggle | [u/d]: Move | [Enter]: Save | [Esc]: Cancel "
            safe_addstr(stdscr, 0, 0, header_text.ljust(w)[:w-1], c_header)
            
            options = cols_config
            for i, cfg in enumerate(options):
                prefix = "[x]" if cfg.get("visible", True) else "[ ]"
                line = f" {i+1}. {prefix} {cfg['label']} ({cfg['key']})"
                if i == settings_idx:
                    safe_addstr(stdscr, 2 + i, 2, line.ljust(w-4), c_selected)
                else:
                    safe_addstr(stdscr, 2 + i, 2, line, c_default)
            
            footer_text = f" Theme: {'Light' if is_light_theme else 'Dark'} | Split-View: {'ON' if config['split_mode'] else 'OFF'} "
            safe_addstr(stdscr, h - 1, 0, footer_text.ljust(w)[:w-1], c_footer)

        elif in_detail and not config["split_mode"]:
            # --- FULL-SCREEN DETAIL VIEW ---
            header_text = f" DETAILS | {rows[current_row].get('company')} - {rows[current_row].get('role')} "
            safe_addstr(stdscr, 0, 0, header_text.ljust(w)[:w-1], c_header)
            
            max_disp = h - 3
            if detail_idx < detail_scroll: detail_scroll = detail_idx
            elif detail_idx >= detail_scroll + max_disp: detail_scroll = detail_idx - max_disp + 1

            for i in range(max_disp):
                idx = detail_scroll + i
                if idx >= len(detail_lines): break
                line_text, fpath = detail_lines[idx]
                line_text = line_text[:w-2]
                
                attr = c_default
                if line_text.startswith("===") or line_text.startswith("COMPANY:") or line_text.startswith("ROLE:"):
                    attr = c_highlight
                if idx == detail_idx:
                    attr = c_selected
                elif fpath:
                    attr = c_highlight
                
                safe_addstr(stdscr, 2 + i, 1, line_text, attr)
            
            progress = int((detail_scroll / max(1, len(detail_lines) - max_disp)) * 100) if len(detail_lines) > max_disp else 100
            footer_text = f" [j/k]: Nav | [Enter]: View File | [b, ESC]: Back | {progress}% "
            safe_addstr(stdscr, h - 1, 0, footer_text.ljust(w)[:w-1], c_footer)
            
        else:
            # --- LIST / SPLIT VIEW ---
            is_split = config["split_mode"] and active_selection_idx is not None
            list_w = (w // 2) if is_split else w
            det_w = (w - list_w - 1) if is_split else 0
            
            visible_cols = [c for c in cols_config if c.get("visible", True)]
            
            # Header
            header_text = f" AI JOB TRACKER | {len(rows)} Apps "
            if is_split: header_text += f"| Focus: {focused_pane} "
            # Provenance is part of the header, not a hidden status: a stale
            # cache must never be mistaken for the live record.
            header_text += f"| {DATA_META.get('label', 'unknown')} "
            pending = DATA_META.get("pendingProposals") or 0
            if pending:
                header_text += f"| {pending} to review "
            safe_addstr(stdscr, 0, 0, header_text.ljust(list_w)[:list_w], c_header)
            
            # Column fitting
            total_fixed = 0
            calc_widths = []
            for cfg in visible_cols:
                if cfg["key"] == "role":
                    calc_widths.append(0)
                else:
                    cw = col_widths[cfg["key"]]
                    calc_widths.append(cw)
                    total_fixed += cw + 3
            
            role_w = list_w - total_fixed - 2
            for i, cfg in enumerate(visible_cols):
                if cfg["key"] == "role": calc_widths[i] = max(0, role_w)
            
            # Column headers
            col_parts = []
            for i, cfg in enumerate(visible_cols):
                arrow = "v" if sort_dirs.get(cfg["key"]) else "^"
                label = f"[{i+1}] {cfg['label']} {arrow if (sort_idx % len(visible_cols)) == i else ''}"
                col_parts.append(f"{label[:calc_widths[i]]:<{calc_widths[i]}}")
            
            header_line = " " + " | ".join(col_parts)
            safe_addstr(stdscr, 2, 0, header_line[:list_w], c_highlight)
            safe_addstr(stdscr, 3, 0, ("-" * list_w)[:list_w], c_default)
            
            max_disp = h - 6
            if current_row < list_scroll: list_scroll = current_row
            elif current_row >= list_scroll + max_disp: list_scroll = current_row - max_disp + 1
                
            for i in range(max_disp):
                idx = list_scroll + i
                if idx >= len(rows): break
                row = rows[idx]
                row_parts = []
                for j, cfg in enumerate(visible_cols):
                    val = str(row.get(cfg["key"], "") or "")
                    if cfg["key"] == "fit_rating" and not val:
                        val = "?? RECALC"
                    width = calc_widths[j]
                    row_parts.append(f"{val[:width]:<{width}}")
                
                line = " " + " | ".join(row_parts)
                attr = c_selected if idx == current_row else (c_highlight if idx == active_selection_idx else c_default)
                safe_addstr(stdscr, 4 + i, 0, line.ljust(list_w)[:list_w], attr)
            
            footer_text = " [j/k]: Nav | [Enter]: Detail | [r]: Recalc | [Tab]: Focus | [c]: Config | [t]: Theme | [q]: Quit "
            safe_addstr(stdscr, h - 1, 0, footer_text.ljust(w)[:w-1], c_footer)
            
            if is_split:
                for i in range(1, h - 1): safe_addstr(stdscr, i, list_w, "|", c_default)
                det_header = f" DETAILS: {rows[active_selection_idx]['company']} | [Enter]: View File "
                safe_addstr(stdscr, 0, list_w+1, det_header.ljust(det_w)[:det_w], c_header if focused_pane=="DETAILS" else c_highlight)
                
                max_det_disp = h - 3
                if detail_idx < detail_scroll: detail_scroll = detail_idx
                elif detail_idx >= detail_scroll + max_det_disp: detail_scroll = detail_idx - max_det_disp + 1

                for i in range(max_det_disp):
                    idx = detail_scroll + i
                    if idx >= len(detail_lines): break
                    line_text, fpath = detail_lines[idx]
                    
                    attr = c_default
                    if idx == detail_idx and focused_pane == "DETAILS":
                        attr = c_selected
                    elif fpath:
                        attr = c_highlight

                    safe_addstr(stdscr, 2+i, list_w+2, line_text[:det_w-2], attr)

        stdscr.refresh()
        key = stdscr.getch()
        
        if key == ord('q'):
            if in_file_view:
                in_file_view = False
                continue
            else:
                break
        if key == 27: # ESC
            if in_file_view: in_file_view = False
            elif in_settings: in_settings = False
            elif in_detail: in_detail = False
            elif active_selection_idx is not None: active_selection_idx = None
            continue

        if in_file_view:
            if key in [curses.KEY_DOWN, ord('j')]: file_view_scroll = min(len(file_view_lines)-(h-3), file_view_scroll+1)
            elif key in [curses.KEY_UP, ord('k')]: file_view_scroll = max(0, file_view_scroll-1)
        elif in_settings:
            if key in [curses.KEY_DOWN, ord('j')]: settings_idx = (settings_idx + 1) % len(cols_config)
            elif key in [curses.KEY_UP, ord('k')]: settings_idx = (settings_idx - 1) % len(cols_config)
            elif key == ord(' '): cols_config[settings_idx]["visible"] = not cols_config[settings_idx].get("visible", True)
            elif key == ord('u') and settings_idx > 0:
                cols_config[settings_idx], cols_config[settings_idx-1] = cols_config[settings_idx-1], cols_config[settings_idx]
                settings_idx -= 1
            elif key == ord('d') and settings_idx < len(cols_config) - 1:
                cols_config[settings_idx], cols_config[settings_idx+1] = cols_config[settings_idx+1], cols_config[settings_idx]
                settings_idx += 1
            elif key in [10, 13]:
                config["columns"] = cols_config
                save_config(config)
                in_settings = False
        elif in_detail and not config["split_mode"]:
            if key == ord('r'):
                target_idx = active_selection_idx if active_selection_idx is not None else current_row
                write_recalc_request(rows[target_idx])
                break
            if key in [ord('b'), ord('q')]: in_detail = False
            elif key in [curses.KEY_DOWN, ord('j')]:
                detail_idx = min(len(detail_lines)-1, detail_idx+1)
            elif key in [curses.KEY_UP, ord('k')]:
                detail_idx = max(0, detail_idx-1)
            elif key in [10, 13]:
                _, fpath = detail_lines[detail_idx]
                if fpath:
                    if fpath.startswith("http"):
                        import webbrowser
                        webbrowser.open(fpath)
                    else:
                        file_view_title = os.path.basename(fpath)
                        try:
                            if fpath.lower().endswith('.pdf'):
                                file_view_lines = ["[Binary PDF File - Cannot display content in TUI]", f"Path: {fpath}"]
                            else:
                                with open(fpath, 'r', errors='replace') as f:
                                    file_view_lines = f.read().splitlines()
                            in_file_view = True
                            file_view_scroll = 0
                        except Exception as e:
                            file_view_lines = [f"Error reading file: {e}"]
                            in_file_view = True
        else:
            if key == ord('c'): in_settings = True
            elif key == ord('r'):
                target_idx = active_selection_idx if active_selection_idx is not None else current_row
                write_recalc_request(rows[target_idx])
                break
            elif key == ord('t'):
                is_light_theme = not is_light_theme
                config["theme"] = "light" if is_light_theme else "dark"
                init_colors(is_light_theme, no_color)
                stdscr.bkgd(' ', curses.color_pair(4))
                save_config(config)
            elif key == ord('\t'):
                if active_selection_idx is not None: focused_pane = "DETAILS" if focused_pane == "LIST" else "LIST"
            elif key in [curses.KEY_DOWN, ord('j')]:
                if focused_pane == "LIST": current_row = min(len(rows)-1, current_row+1)
                else: detail_idx = min(len(detail_lines)-1, detail_idx+1)
            elif key in [curses.KEY_UP, ord('k')]:
                if focused_pane == "LIST": current_row = max(0, current_row-1)
                else: detail_idx = max(0, detail_idx-1)
            elif key in [ord('1'), ord('2'), ord('3'), ord('4'), ord('5')]:
                v_cols = [c for c in cols_config if c.get("visible", True)]
                idx = (key - ord('1'))
                if idx < len(v_cols):
                    sort_idx = idx
                    k = v_cols[idx]["key"]
                    sort_dirs[k] = not sort_dirs.get(k, False)
                    sort_data()
            elif key in [10, 13]:
                if config["split_mode"]:
                    if focused_pane == "DETAILS":
                        _, fpath = detail_lines[detail_idx]
                        if fpath:
                            if fpath.startswith("http"):
                                import webbrowser
                                webbrowser.open(fpath)
                            else:
                                file_view_title = os.path.basename(fpath)
                                try:
                                    if fpath.lower().endswith('.pdf'):
                                        file_view_lines = ["[Binary PDF File - Cannot display content in TUI]", f"Path: {fpath}"]
                                    else:
                                        with open(fpath, 'r', errors='replace') as f:
                                            file_view_lines = f.read().splitlines()
                                    in_file_view = True
                                    file_view_scroll = 0
                                except Exception as e:
                                    file_view_lines = [f"Error reading file: {e}"]
                                    in_file_view = True
                    else:
                        if active_selection_idx == current_row:
                            active_selection_idx = None
                            focused_pane = "LIST"
                        else:
                            active_selection_idx = current_row
                            detail_lines = load_detail_lines(rows[active_selection_idx], w//2 - 2)
                            detail_scroll = 0
                            detail_idx = 0
                else:
                    detail_lines = load_detail_lines(rows[current_row], w-2)
                    in_detail = True
                    detail_scroll = 0
                    detail_idx = 0

if __name__ == "__main__":
    try: curses.wrapper(main)
    except KeyboardInterrupt: pass
