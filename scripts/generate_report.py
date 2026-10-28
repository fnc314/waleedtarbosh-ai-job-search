import csv
import html
import os
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

tracker_path = 'job_search_tracker.csv'
if not os.path.exists(tracker_path):
    candidate = REPO_ROOT / 'job_search_tracker.csv'
    if candidate.exists():
        tracker_path = str(candidate)

records = []
if os.path.exists(tracker_path):
    with open(tracker_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append(row)

for r in records:
    st = r.get('status', '').lower().strip()
    if st == 'applied':
        r['n_status'] = 'Active'
    elif st == 'interview':
        r['n_status'] = 'Interview'
    elif st == 'offer':
        r['n_status'] = 'Offer'
    elif st == 'hired':
        r['n_status'] = 'Hired'
    elif st in ['rejected', 'no_response', 'no response', 'offer_declined', 'interview_only', 'withdrawn']:
        r['n_status'] = 'Rejected/Closed'
    else:
        r['n_status'] = 'Active'

total = len(records)
counts = Counter(r['n_status'] for r in records)
active = counts['Active']
interview = counts['Interview']
offer = counts['Offer']
hired = counts['Hired']
rejected = counts['Rejected/Closed']

# Funnel
past_resume = interview + offer + hired
funnel_rate = round((past_resume / total) * 100) if total else 0

sectors = Counter(r.get('sector', '').strip() for r in records if r.get('sector', '').strip())

table_rows = []
for r in sorted(records, key=lambda x: (x.get('date', ''), x.get('company', '')), reverse=True):
    date = html.escape(r.get('date', '—') or '—')
    comp = html.escape(r.get('company', '—') or '—')
    role = html.escape(r.get('role', '—') or '—')
    sect = html.escape(r.get('sector', '—') or '—')
    chan = html.escape(r.get('channel', '—') or '—')
    stat = html.escape(r.get('status', '—') or '—')
    notes = html.escape(r.get('notes', '—') or '—')
    short_notes = notes[:80] + '...' if len(notes) > 80 else notes
    src = r.get('source') or ''
    if src.startswith('http'):
        src_td = f'<a href="{html.escape(src)}" target="_blank">Link</a>'
    else:
        src_td = '—'
        
    table_rows.append(f"""<tr>
        <td>{date}</td>
        <td>{comp}</td>
        <td>{role}</td>
        <td>{sect}</td>
        <td>{chan}</td>
        <td><span class="badge {stat}">{stat}</span></td>
        <td title="{notes}">{short_notes}</td>
        <td>{src_td}</td>
    </tr>""")

html_out = f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<title>Job Search Dashboard</title>
<style>
  :root {{
    --active: #3b82f6;
    --interview: #f59e0b;
    --offer: #8b5cf6;
    --hired: #22c55e;
    --rejected: #ef4444;
  }}
  body {{ font-family: system-ui, sans-serif; background: #f9fafb; margin: 2rem; color: #111827; }}
  .header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 2rem; }}
  .cards {{ display: flex; gap: 1rem; margin-bottom: 2rem; flex-wrap: wrap; }}
  .card {{ background: white; padding: 1.5rem; border-radius: 0.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); flex: 1; min-width: 150px; border-left: 4px solid #ccc; }}
  .card h2 {{ margin: 0; font-size: 2rem; }}
  .card p {{ margin: 0; font-size: 0.875rem; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em; }}
  .card.active {{ border-left-color: var(--active); }}
  .card.interview {{ border-left-color: var(--interview); }}
  .card.offer {{ border-left-color: var(--offer); }}
  .card.rejected {{ border-left-color: var(--rejected); }}
  
  .charts {{ display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; margin-bottom: 2rem; }}
  @media (max-width: 900px) {{ .charts {{ grid-template-columns: 1fr; }} }}
  .chart-card {{ background: white; padding: 1.5rem; border-radius: 0.5rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
  .chart-card h3 {{ margin-top: 0; margin-bottom: 1rem; }}
  
  table {{ width: 100%; border-collapse: collapse; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 0.5rem; overflow: hidden; }}
  th, td {{ padding: 0.75rem 1rem; text-align: left; border-bottom: 1px solid #e5e7eb; }}
  th {{ background: #f3f4f6; font-weight: 600; }}
  tr:last-child td {{ border-bottom: none; }}
  tr:nth-child(even) {{ background: #f9fafb; }}
  
  .badge {{ display: inline-block; padding: 0.25rem 0.5rem; border-radius: 9999px; font-size: 0.75rem; font-weight: 600; text-transform: uppercase; }}
  .badge.applied, .badge.active {{ background: #dbeafe; color: #1e40af; }}
  .badge.interview {{ background: #fef3c7; color: #92400e; }}
  .badge.offer {{ background: #ede9fe; color: #5b21b6; }}
  .badge.hired {{ background: #dcfce7; color: #166534; }}
  .badge.rejected {{ background: #fee2e2; color: #991b1b; }}
</style>
</head>
<body>
  <div class="header">
    <h1>Job Search Dashboard</h1>
    <p>Generated: {datetime.now().isoformat()[:10]}</p>
  </div>
  
  <div class="cards">
    <div class="card">
      <h2>{total}</h2><p>Total</p>
    </div>
    <div class="card active">
      <h2>{active}</h2><p>Active</p>
    </div>
    <div class="card interview">
      <h2>{interview}</h2><p>Interview</p>
    </div>
    <div class="card offer">
      <h2>{offer}</h2><p>Offer</p>
    </div>
    <div class="card rejected">
      <h2>{rejected}</h2><p>Rejected/Closed</p>
    </div>
  </div>

  <div class="charts">
    <div class="chart-card">
      <h3>Funnel Progress</h3>
      <svg width="100%" height="60" role="img" aria-label="Funnel: {funnel_rate}% progressed past resume screen">
        <rect width="100%" height="30" y="15" fill="#e5e7eb" rx="5" />
        <rect width="{funnel_rate}%" height="30" y="15" fill="var(--active)" rx="5" />
        <text x="50%" y="35" fill="white" font-size="14" font-weight="bold" text-anchor="middle">{funnel_rate}% to Interview+</text>
      </svg>
    </div>
    <div class="chart-card">
      <h3>By Sector</h3>
      <ul>
        {''.join(f"<li>{html.escape(s)}: {c}</li>" for s, c in sectors.items())}
      </ul>
    </div>
  </div>

  <div style="overflow-x: auto;">
    <table>
      <thead>
        <tr>
          <th>Date</th><th>Company</th><th>Role</th><th>Sector</th><th>Channel</th><th>Status</th><th>Notes</th><th>Source</th>
        </tr>
      </thead>
      <tbody>
        {''.join(table_rows)}
      </tbody>
    </table>
  </div>
</body>
</html>"""

output_paths = []
if len(sys.argv) > 1 and not sys.argv[1].startswith('-'):
    output_paths.append(sys.argv[1])
else:
    reports_dir = REPO_ROOT / 'reports'
    reports_dir.mkdir(parents=True, exist_ok=True)
    output_paths.append(str(reports_dir / 'application-dashboard.html'))
    
    ui_web_dir = REPO_ROOT / 'ui' / 'web'
    if ui_web_dir.exists():
        output_paths.append(str(ui_web_dir / 'application-dashboard.html'))

for out_path in output_paths:
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(html_out)
    print(f"Generated dashboard: {out_path}")
