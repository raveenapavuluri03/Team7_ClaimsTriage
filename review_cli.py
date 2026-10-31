"""Reviewer / operations command line for the human checkpoint.

  python review_cli.py list                         show all reviews
  python review_cli.py show R-abc123                what the reviewer sees
  python review_cli.py decide R-abc123 approve --reviewer adjuster_1 --note "docs ok"   (writes an event file to inbox/)
  python review_cli.py worker                       recover, then process everything in inbox/
  python review_cli.py sweep --hours 49             timer event; pretend 49 hours have passed
  python review_cli.py ledger                       what the simulated payment / notification systems recorded
"""
import argparse
from datetime import datetime, timedelta

from src import review
from src.store import connect

ap = argparse.ArgumentParser()
sub = ap.add_subparsers(dest="cmd", required=True)
sub.add_parser("list")
p = sub.add_parser("show"); p.add_argument("review_id")
p = sub.add_parser("decide")
p.add_argument("review_id"); p.add_argument("decision", choices=["approve", "reject"])
p.add_argument("--reviewer", default="adjuster_1"); p.add_argument("--note", default="")
p.add_argument("--event-id", default=None); p.add_argument("--ack-fallback", action="store_true")
sub.add_parser("worker")
p = sub.add_parser("sweep"); p.add_argument("--hours", type=float, default=0)
sub.add_parser("ledger")
a = ap.parse_args()

if a.cmd == "list":
    for r in review.list_reviews():
        print(f"{r['review_id']}  {r['claim_id']:<10} {r['status']:<11} {r['action']} ${r['amount'] or 0:>9,.2f}  "
              f"v{r['content_hash']}  expires {r['expires_at']}  by {r['decided_by'] or '-'}")
elif a.cmd == "show":
    print(review.render_review(a.review_id))
elif a.cmd == "decide":
    ev = review.make_event(a.review_id, a.decision, a.reviewer, a.note, a.event_id, a.ack_fallback)
    print("Event written to", review.drop_event_file(ev), "- now run: python review_cli.py worker")
elif a.cmd == "worker":
    for name, outcome in review.process_inbox():
        print(f"{name}: {outcome}")
elif a.cmd == "sweep":
    review.sweep(datetime.now() + timedelta(hours=a.hours))
    print("sweep done")
elif a.cmd == "ledger":
    with connect() as c:
        for t in ("payments", "notifications"):
            print(f"-- {t}")
            for r in c.execute(f"SELECT * FROM {t}"):
                print(dict(r))