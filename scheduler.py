"""Runs reminders and recurring tasks in the background while Kakarot is running."""
import datetime
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger

import db

log = logging.getLogger("kakarot.scheduler")
sched = BackgroundScheduler(job_defaults={"coalesce": True, "misfire_grace_time": 3600, "max_instances": 1})


def notify(title, body):
    """Windows desktop pop-up (falls back silently if unavailable)."""
    try:
        from plyer import notification
        notification.notify(title=title[:60], message=(body or "")[:240], app_name="Kakarot", timeout=10)
    except Exception as e:
        log.debug("notification failed: %s", e)


def _fire_reminder(job_id):
    rows = db.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        return
    msg = rows[0]["prompt"]
    db.add_inbox("⏰ Reminder", msg)
    notify("Reminder", msg)
    db.x("DELETE FROM jobs WHERE id=?", (job_id,))


def _run_task(job_id):
    rows = db.q("SELECT * FROM jobs WHERE id=?", (job_id,))
    if not rows:
        return
    j = rows[0]
    from agent import run_agent
    try:
        reply, _ = run_agent(j["prompt"], background=True)
    except Exception as e:
        reply = f"Task failed: {e}"
    db.add_inbox(f"🔁 {j['name']}", reply)
    notify(j["name"], reply)


def _register(j):
    if j["kind"] == "reminder":
        at = datetime.datetime.fromisoformat(j["run_at"])
        if at <= datetime.datetime.now():  # missed while PC/app was off
            at = datetime.datetime.now() + datetime.timedelta(seconds=5)
        sched.add_job(_fire_reminder, DateTrigger(run_date=at), args=[j["id"]], id=j["id"], replace_existing=True)
    else:
        sched.add_job(_run_task, CronTrigger.from_crontab(j["cron"]), args=[j["id"]], id=j["id"],
                      replace_existing=True)
    job = sched.get_job(j["id"])
    return job.next_run_time.strftime("%a %d %b %H:%M") if job and job.next_run_time else "?"


def add_job(j):
    if j["kind"] == "task":
        CronTrigger.from_crontab(j["cron"])  # validate before saving
    db.x("INSERT INTO jobs(id, name, prompt, cron, run_at, kind, created) VALUES(?,?,?,?,?,?,?)",
         (j["id"], j["name"], j["prompt"], j["cron"], j["run_at"], j["kind"], db.now()))
    return _register(j)


def remove_job(job_id):
    if not db.q("SELECT id FROM jobs WHERE id=?", (job_id,)):
        raise KeyError(f"No scheduled item with id {job_id}")
    db.x("DELETE FROM jobs WHERE id=?", (job_id,))
    if sched.get_job(job_id):
        sched.remove_job(job_id)


def list_jobs():
    out = []
    for j in db.q("SELECT * FROM jobs ORDER BY created"):
        job = sched.get_job(j["id"])
        nxt = job.next_run_time.strftime("%a %d %b %H:%M") if job and job.next_run_time else "-"
        out.append({"id": j["id"], "kind": j["kind"], "name": j["name"],
                    "schedule": j["cron"] or j["run_at"], "next_run": nxt,
                    "task": j["prompt"] if j["kind"] == "task" else ""})
    return out


def start():
    if sched.running:
        return
    sched.start()
    for j in db.q("SELECT * FROM jobs"):
        try:
            _register(j)
        except Exception as e:
            log.error("could not restore job %s: %s", j["id"], e)
