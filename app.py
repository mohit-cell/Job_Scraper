"""Streamlit UI for Resume Creator.  Run with:  streamlit run app.py"""
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from pipeline import Config, google_credentials, run_pipeline

load_dotenv()
st.set_page_config(page_title="Resume Creator", page_icon="📄", layout="wide")

STATUS_ICON = {
    "queued": "⚪ Queued", "evaluating": "🔎 Checking fit", "skipped": "⏭️ Not a fit",
    "tailoring": "✍️ Tailoring resume", "committing": "⬆️ Pushing to GitHub",
    "compiling": "🛠️ Compiling PDF", "uploading": "☁️ Uploading to Drive",
    "logging": "📝 Logging to sheet", "done": "✅ Done", "error": "❌ Error",
}
STAGE_ICON = {"running": "⏳", "done": "✅", "skipped": "➖", "error": "❌", "pending": "⚪"}
STAGES = ["Scrape LinkedIn jobs", "Remove already-processed jobs", "Load base resume", "Evaluate & generate resumes"]

# --------------------------------------------------------------------------- #
# Session state
# --------------------------------------------------------------------------- #
ss = st.session_state
ss.setdefault("creds", None)
ss.setdefault("results", {})      # index -> JobResult
ss.setdefault("metrics", None)
ss.setdefault("stages", {})
ss.setdefault("google_tried", False)

if not ss.google_tried:
    ss.google_tried = True
    try:
        if os.path.exists("token.json"):
            ss.creds = google_credentials(interactive=False)
    except Exception:
        ss.creds = None

# --------------------------------------------------------------------------- #
# Sidebar: parameters
# --------------------------------------------------------------------------- #
d = Config()
with st.sidebar:
    st.title("⚙️ Parameters")

    with st.expander("🔑 API keys", expanded=not os.getenv("OPENAI_API_KEY")):
        apify_token = st.text_input("Apify token", os.getenv("APIFY_TOKEN", "apify_api_jUq3mzM3Vc2NGrVPgWnVN5DGwDnU9d3Aobwz"), type="password")
        openai_key = st.text_input("OpenAI API key", os.getenv("OPENAI_API_KEY", "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"), type="password")
        github_token = st.text_input("GitHub token (contents: write)", os.getenv("GITHUB_TOKEN", "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"), type="password")

    with st.expander("🔎 Job search", expanded=True):
        keywords = st.text_input("Job keywords", d.keywords)
        location = st.text_input("Location", d.location)
        date_posted = st.selectbox("Date posted", ["last24h", "lastHour", "last7d", "last30d", "anytime"])
        experience_level = st.multiselect("Experience level", ["entry", "internship", "associate", "mid-senior", "director", "executive"], default=d.experience_level)
        job_type = st.multiselect("Job type", ["fulltime", "parttime", "contract", "internship", "temporary", "volunteer", "other"], default=d.job_type)
        work_type = st.multiselect("Work type", ["onsite", "remote", "hybrid"], default=d.work_type)
        st.caption("Full job descriptions are enabled for fit checks and resume tailoring. Leave a filter empty to include all values.")
        job_count = st.number_input("Jobs to scrape", 1, 1000, d.job_count, step=1)
        max_eval = st.number_input("Max jobs to evaluate (0 = all)", 0, 1000, 10, step=5,
                                   help="Each evaluated job costs one OpenAI call; each fit costs another.")
        max_resumes = st.number_input("Max resumes to generate (0 = no limit)", 0, 1000, 3, step=1)

    with st.expander("📄 Base resume", expanded=False):
        resume_src = st.radio("Source", ["Google Doc", "Paste text"], horizontal=True)
        if resume_src == "Google Doc":
            resume_doc_id = st.text_input("Google Doc ID", d.resume_doc_id)
            resume_text = ""
        else:
            resume_doc_id = d.resume_doc_id
            resume_text = st.text_area("Resume text", height=200)

    with st.expander("🧠 Model", expanded=False):
        model = st.text_input("OpenAI model", d.model)

    with st.expander("🚀 Publishing", expanded=False):
        publish = st.toggle("Publish (GitHub → PDF → Drive → Sheet)", value=True,
                            help="Off = preview mode: only evaluate jobs and generate LaTeX in the app.")
        skip_dedupe = st.toggle("Skip duplicate check", value=False,
                                help="Don't look in the sheet for already-processed jobs.")
        sheet_id = st.text_input("Google Sheet ID", d.sheet_id)
        sheet_gid = st.number_input("Sheet tab gid", 0, value=d.sheet_gid)
        drive_folder_id = st.text_input("Drive folder ID", d.drive_folder_id)
        gh_owner = st.text_input("GitHub owner", d.github_owner)
        gh_repo = st.text_input("GitHub repo (public)", d.github_repo)

    cfg_probe = Config(resume_text_override=resume_text, skip_dedupe=skip_dedupe, publish=publish)
    if cfg_probe.needs_google():
        st.subheader("Google account")
        if ss.creds:
            st.success("Signed in to Google")
            if st.button("Sign out"):
                if os.path.exists("token.json"):
                    os.remove("token.json")
                ss.creds = None
                st.rerun()
        else:
            up = st.file_uploader("OAuth client_secret.json", type="json")
            if up is not None:
                Path("client_secret.json").write_bytes(up.getvalue())
            if st.button("Sign in with Google", disabled=not Path("client_secret.json").exists()):
                try:
                    ss.creds = google_credentials(interactive=True)
                    st.rerun()
                except Exception as e:
                    st.error(f"Google sign-in failed: {e}")
            st.caption("Opens a browser tab on the machine running Streamlit.")

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #
st.title("📄 Resume Creator")
st.caption("Scrape jobs → check fit → tailor your LaTeX resume → compile PDF → track in Google Sheets")

tab_run, tab_prompts = st.tabs(["▶️ Run", "📝 Prompts & template"])

with tab_prompts:
    st.caption("Edit the prompts and LaTeX template used for this run. {{RESUME}} and {{JOB}} are placeholders.")
    relevancy_system = st.text_area("Fit check – system prompt", d.relevancy_system, height=220)
    relevancy_user = st.text_area("Fit check – user prompt", d.relevancy_user, height=220)
    customize_system = st.text_area("Tailoring – system prompt", d.customize_system, height=260)
    customize_tail = st.text_area("Tailoring – extra instructions (appended after the template)", d.customize_user_tail, height=200)
    resume_template = st.text_area("LaTeX resume template", d.resume_template, height=400)

with tab_run:
    mode_txt = "**Publish mode**" if publish else "**Preview mode** (nothing is pushed, uploaded or logged)"
    st.markdown(f"Mode: {mode_txt}")
    start = st.button("🚀 Start", type="primary")

    stage_box = st.container()
    metrics_box = st.container()
    progress_slot = st.empty()
    table_slot = st.empty()
    details_box = st.container()


def render_stages():
    with stage_box:
        lines = []
        for name in STAGES:
            state, detail = ss.stages.get(name, ("pending", ""))
            lines.append(f"{STAGE_ICON[state]} **{name}** — {detail}" if detail else f"{STAGE_ICON[state]} **{name}**")
        stage_slot.markdown("\n\n".join(lines))


def render_metrics():
    m = ss.metrics
    if not m:
        return
    cols = metrics_slot.columns(6)
    for col, (label, key) in zip(cols, [("Scraped", "scraped"), ("New", "new"), ("Evaluated", "evaluated"),
                                        ("Fits", "fits"), ("Resumes", "resumes"), ("Errors", "errors")]):
        col.metric(label, m[key])


def render_table():
    if not ss.results:
        return
    rows = []
    for i in sorted(ss.results):
        r = ss.results[i]
        rows.append({
            "Status": STATUS_ICON.get(r.status, r.status), "Title": r.title, "Company": r.company,
            "Fit": "—" if r.verdict is None else ("✅" if r.verdict else "❌"),
            "Reason": r.reason or r.error, "Job": r.link, "Resume": r.resume_url or None,
        })
    table_slot.dataframe(
        pd.DataFrame(rows), use_container_width=True, hide_index=True,
        column_config={"Job": st.column_config.LinkColumn("Job", display_text="open"),
                       "Resume": st.column_config.LinkColumn("Resume", display_text="PDF"),
                       "Reason": st.column_config.TextColumn("Reason", width="large")},
    )


def render_details():
    fits = [(i, r) for i, r in sorted(ss.results.items()) if r.latex or r.error]
    if not fits:
        return
    with details_box:
        st.subheader("Generated resumes")
        for i, r in fits:
            with st.expander(f"{STATUS_ICON.get(r.status, r.status)} · {r.title} @ {r.company}"):
                if r.error:
                    st.error(r.error)
                st.markdown(f"**Why it's a fit:** {r.reason}")
                links = []
                if r.link: links.append(f"[Job post]({r.link})")
                if r.apply_url: links.append(f"[Apply]({r.apply_url})")
                if r.resume_url: links.append(f"[Resume PDF in Drive]({r.resume_url})")
                if links: st.markdown(" · ".join(links))
                c1, c2, _ = st.columns([1, 1, 4])
                if r.latex:
                    c1.download_button("⬇️ .tex", r.latex, file_name=f"{r.company}_{r.id}.tex", key=f"tex{i}")
                if r.pdf:
                    c2.download_button("⬇️ PDF", r.pdf, file_name=f"{r.company}_{r.id}.pdf",
                                       mime="application/pdf", key=f"pdf{i}")
                t1, t2 = st.tabs(["LaTeX", "Job description"])
                t1.code(r.latex, language="latex")
                t2.text(r.description)


def validate(cfg: Config):
    problems = []
    if not cfg.apify_token: problems.append("Apify token is missing.")
    if not cfg.openai_api_key: problems.append("OpenAI API key is missing.")
    if cfg.publish and not cfg.github_token: problems.append("GitHub token is required in publish mode.")
    if cfg.resume_doc_id == "" and not cfg.resume_text_override: problems.append("Provide a resume doc ID or text.")
    if cfg.needs_google() and not ss.creds: problems.append("Sign in to Google (sidebar) — needed for the resume doc, sheet or Drive.")
    return problems


with tab_run:
    stage_slot = stage_box.empty()
    metrics_slot = metrics_box.empty()

    if start:
        cfg = Config(
            apify_token=apify_token, openai_api_key=openai_key, github_token=github_token,
            keywords=keywords, location=location, date_posted=date_posted,
            experience_level=experience_level, job_type=job_type, work_type=work_type,
            job_count=int(job_count),
            max_jobs_to_evaluate=int(max_eval), max_resumes=int(max_resumes),
            resume_doc_id=resume_doc_id, resume_text_override=resume_text.strip(),
            sheet_id=sheet_id, sheet_gid=int(sheet_gid), drive_folder_id=drive_folder_id,
            github_owner=gh_owner, github_repo=gh_repo, model=model,
            skip_dedupe=skip_dedupe, publish=publish,
            relevancy_system=relevancy_system, relevancy_user=relevancy_user,
            customize_system=customize_system, resume_template=resume_template,
            customize_user_tail=customize_tail,
        )
        problems = validate(cfg)
        if problems:
            for p in problems:
                st.error(p)
        else:
            ss.results, ss.stages, ss.metrics = {}, {}, None
            bar = progress_slot.progress(0.0, text="Starting...")
            for ev in run_pipeline(cfg, ss.creds):
                if ev["event"] == "stage":
                    ss.stages[ev["name"]] = (ev["state"], ev["detail"])
                    render_stages()
                    if ev["state"] == "error":
                        st.error(f"{ev['name']}: {ev['detail']}")
                elif ev["event"] == "metrics":
                    ss.metrics = ev["metrics"]
                    render_metrics()
                elif ev["event"] == "job":
                    ss.results[ev["index"]] = ev["job"]
                    done = ev["index"] + (1 if ev["job"].status in ("done", "skipped", "error") else 0)
                    bar.progress(done / ev["total"], text=f"Job {ev['index'] + 1} / {ev['total']}: "
                                 f"{STATUS_ICON.get(ev['job'].status)} — {ev['job'].title[:60]}")
                    render_table()
            progress_slot.empty()
            render_details()
            if ss.metrics:
                st.subheader("Funnel")
                m = ss.metrics
                st.bar_chart(pd.DataFrame({"jobs": [m["scraped"], m["new"], m["evaluated"], m["fits"], m["resumes"]]},
                                          index=["Scraped", "New", "Evaluated", "Fits", "Resumes"]))
    else:
        # show the previous run (state survives reruns such as clicking a download button)
        render_stages(); render_metrics(); render_table(); render_details()
