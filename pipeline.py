"""
Core pipeline for Resume Creator (ported from the n8n workflow).

`run_pipeline()` is a generator that yields progress events, so any front-end
(Streamlit, CLI, ...) can visualise what is happening.

Event shapes
------------
{"event": "stage", "name": str, "state": "running"|"done"|"skipped"|"error", "detail": str}
{"event": "job",   "index": int, "total": int, "job": JobResult}
{"event": "metrics", "metrics": dict}
{"event": "finished"}
"""
from __future__ import annotations

import base64
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import requests

PROMPTS = Path(__file__).parent / "prompts"

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
]
APIFY_ACTOR_ID = "blackfalcondata/linkedin-job-scraper"


def read_prompt(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


@dataclass
class Config:
    # secrets
    apify_token: str = ""
    openai_api_key: str = ""
    github_token: str = ""
    # scraping
    keywords: str = "software engineer"
    location: str = "United States"
    date_posted: str = "last24h"
    experience_level: list[str] = field(default_factory=lambda: ["entry"])
    job_type: list[str] = field(default_factory=lambda: ["fulltime"])
    work_type: list[str] = field(default_factory=lambda: ["onsite"])
    job_count: int = 10
    # limits (0 = no limit)
    max_jobs_to_evaluate: int = 0
    max_resumes: int = 0
    # resume source
    resume_doc_id: str = "1DhY0MGPn29DsYZWRfB2BdsGDrJ6sq5XrvOrw6ly4P-w"
    resume_text_override: str = ""  # if set, Google Doc is not read
    # google sheet / drive
    sheet_id: str = "1dh3U13kqyF7Kn1godCZZ_6QSRROwItz58d5Sf8dW4TE"
    sheet_gid: int = 0
    drive_folder_id: str = "1C0YPEEmJOG_jal_smXJhTyFS7tqS5_BH"
    # github (stores .tex so latexonline.cc can compile it)
    github_owner: str = "mohit-cell"
    github_repo: str = "Job_Scraper"
    # llm
    model: str = "gpt-4.1-mini"
    # behaviour
    skip_dedupe: bool = False   # don't check the sheet for already-processed jobs
    publish: bool = True        # commit to GitHub, compile PDF, upload to Drive, log to sheet
    # prompts (editable in the UI)
    relevancy_system: str = field(default_factory=lambda: read_prompt("relevancy_system.txt"))
    relevancy_user: str = field(default_factory=lambda: read_prompt("relevancy_user.txt"))
    customize_system: str = field(default_factory=lambda: read_prompt("customize_system.txt"))
    resume_template: str = field(default_factory=lambda: read_prompt("resume_template.tex"))
    customize_user_tail: str = field(default_factory=lambda: read_prompt("customize_user_tail.txt"))

    def needs_google(self) -> bool:
        return (not self.resume_text_override) or (not self.skip_dedupe) or self.publish


@dataclass
class JobResult:
    id: str
    title: str = ""
    company: str = ""
    link: str = ""
    apply_url: str = ""
    description: str = ""
    status: str = "queued"   # queued | evaluating | skipped | tailoring | committing | compiling | uploading | logging | done | error
    verdict: Optional[bool] = None
    reason: str = ""
    latex: str = ""
    pdf: bytes = b""
    resume_url: str = ""
    error: str = ""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def fill(template: str, **values: str) -> str:
    """Replace {{KEY}} placeholders (str.format is unsafe because LaTeX has braces)."""
    for key, val in values.items():
        template = template.replace("{{" + key + "}}", val or "")
    return template


def strip_code_fences(text: str) -> str:
    text = text.strip()
    m = re.match(r"^```[a-zA-Z]*\n(.*?)\n```$", text, flags=re.S)
    return m.group(1).strip() if m else text


def google_credentials(client_secrets_path: str = "client_secret.json",
                       token_path: str = "token.json", interactive: bool = True):
    """Load / refresh / create Google OAuth credentials."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if os.path.exists(token_path):
        creds = Credentials.from_authorized_user_file(token_path, GOOGLE_SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    elif interactive:
        flow = InstalledAppFlow.from_client_secrets_file(client_secrets_path, GOOGLE_SCOPES)
        creds = flow.run_local_server(port=0)
    else:
        return None
    Path(token_path).write_text(creds.to_json())
    return creds


# --------------------------------------------------------------------------- #
# Steps
# --------------------------------------------------------------------------- #
def normalize_job(job: dict) -> dict:
    """Adapt Black Falcon output to the fields consumed by the pipeline."""
    job_id = job.get("linkedinJobId") or job.get("jobId")
    if not job_id:
        raise ValueError("Scraper returned a job without linkedinJobId or jobId.")
    return {
        **job,
        "id": str(job_id),
        "title": job.get("title") or "",
        "companyName": job.get("company") or "",
        "link": job.get("jobUrl") or "",
        "applyUrl": job.get("applyUrl") or "",
        "descriptionText": job.get("description") or "",
        "descriptionHtml": job.get("descriptionHtml") or "",
        "companyLinkedinUrl": job.get("companyUrl") or "",
        "applicantsCount": job.get("applicantCount"),
    }


def scrape_jobs(cfg: Config) -> list[dict]:
    from apify_client import ApifyClient
    client = ApifyClient(cfg.apify_token)
    run = client.actor(APIFY_ACTOR_ID).call(
        run_input={
            "keywords": cfg.keywords,
            "location": cfg.location,
            "datePosted": cfg.date_posted,
            "experienceLevel": cfg.experience_level,
            "jobType": cfg.job_type,
            "workType": cfg.work_type,
            "maxResults": cfg.job_count,
            "enrichDetails": True,
            "includeDetails": True,
            "descriptionFormat": "all",
            "compact": False,
            "excludeEmptyFields": True,
            "scrapeCompany": False,
            "incrementalMode": False,
            "allowNonIncrementalFallback": False,
            "discoverRelated": False,
            "earlyApplicant": False,
            "easyApply": False,
            "emitExpired": False,
            "emitUnchanged": False,
            "notifyOnlyChanges": False,
            "removeAgency": False,
            "requireRecruiter": False,
            "requireSalary": False,
            "salaryIncludeUnknown": True,
            "skipReposts": False,
        }
    )
    if run.get("status") != "SUCCEEDED":
        raise RuntimeError(f"Apify scrape did not succeed: {run.get('status', 'unknown status')}")
    return [normalize_job(job) for job in client.dataset(run["defaultDatasetId"]).iterate_items()]


class JobsTracker:
    def __init__(self, creds, cfg: Config):
        from googleapiclient.discovery import build
        self.cfg = cfg
        self.svc = build("sheets", "v4", credentials=creds, cache_discovery=False).spreadsheets()
        meta = self.svc.get(spreadsheetId=cfg.sheet_id, fields="sheets.properties").execute()
        self.title = next(s["properties"]["title"] for s in meta["sheets"]
                          if s["properties"]["sheetId"] == cfg.sheet_gid)
        res = self.svc.values().get(spreadsheetId=cfg.sheet_id, range=f"'{self.title}'!1:1").execute()
        self.headers = (res.get("values") or [[]])[0]

    def existing_job_ids(self) -> set[str]:
        res = self.svc.values().get(spreadsheetId=self.cfg.sheet_id, range=f"'{self.title}'").execute()
        rows = res.get("values", [])
        if not rows or "Job ID" not in rows[0]:
            return set()
        idx = rows[0].index("Job ID")
        return {str(r[idx]) for r in rows[1:] if len(r) > idx and r[idx] != ""}

    def append(self, record: dict) -> None:
        row = [record.get(h, "") for h in self.headers]
        self.svc.values().append(
            spreadsheetId=self.cfg.sheet_id, range=f"'{self.title}'!A1",
            valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [row]},
        ).execute()


def get_resume_text(creds, cfg: Config) -> str:
    from googleapiclient.discovery import build
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    data = drive.files().export(fileId=cfg.resume_doc_id, mimeType="text/plain").execute()
    return data.decode("utf-8") if isinstance(data, bytes) else data


def is_job_a_fit(openai, cfg: Config, resume_text: str, description: str) -> tuple[bool, str]:
    resp = openai.responses.create(
        model=cfg.model,
        input=[
            {"role": "system", "content": fill(cfg.relevancy_system, RESUME=resume_text, JOB=description)},
            {"role": "user", "content": fill(cfg.relevancy_user, RESUME=resume_text, JOB=description)},
        ],
        text={"format": {"type": "json_object"}},
    )
    result = json.loads(resp.output_text)
    return str(result.get("verdict", "")).strip().lower() == "true", result.get("reason", "")


def customize_resume(openai, cfg: Config, description: str) -> tuple[str, str]:
    user_prompt = (
        "#Overview\n\n"
        "I'm looking for jobs. Your task is to customise a provided resume using a provided job description.\n\n"
        "##Context\n\n"
        f"Here is the job description\n\n{description}\n\n--\n\n"
        f"Here is my resume in markdown format: \n\n{cfg.resume_template}\n\n"
        f"{cfg.customize_user_tail}"
    )
    resp = openai.responses.create(
        model=cfg.model,
        input=[{"role": "system", "content": cfg.customize_system},
               {"role": "user", "content": user_prompt}],
    )
    msg = next((o for o in resp.output if o.type == "message"), resp.output[0])
    return msg.id, strip_code_fences(resp.output_text)


def commit_tex_to_github(cfg: Config, file_id: str, latex: str) -> str:
    url = f"https://api.github.com/repos/{cfg.github_owner}/{cfg.github_repo}/contents/{file_id}.tex"
    r = requests.put(
        url,
        headers={"Authorization": f"Bearer {cfg.github_token}", "Accept": "application/vnd.github+json"},
        json={"message": "add file", "content": base64.b64encode(latex.encode()).decode()},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["content"]["name"]


def compile_pdf(cfg: Config, target_name: str, retries: int = 3) -> bytes:
    params = {"git": f"https://github.com/{cfg.github_owner}/{cfg.github_repo}",
              "target": target_name, "command": "pdflatex"}
    last = None
    for attempt in range(1, retries + 1):
        r = requests.get("https://latexonline.cc/compile", params=params, timeout=180)
        if r.ok and r.content[:4] == b"%PDF":
            return r.content
        last = f"HTTP {r.status_code}: {r.text[:300]}"
        time.sleep(5 * attempt)
    raise RuntimeError(f"LaTeX compilation failed: {last}")


def upload_pdf(creds, cfg: Config, name: str, pdf_bytes: bytes) -> str:
    from googleapiclient.discovery import build
    from googleapiclient.http import MediaIoBaseUpload
    drive = build("drive", "v3", credentials=creds, cache_discovery=False)
    f = drive.files().create(
        body={"name": name, "parents": [cfg.drive_folder_id]},
        media_body=MediaIoBaseUpload(io.BytesIO(pdf_bytes), mimetype="application/pdf"),
        fields="id,webViewLink",
    ).execute()
    return f["webViewLink"]


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _stage(name, state, detail=""):
    return {"event": "stage", "name": name, "state": state, "detail": detail}


def run_pipeline(cfg: Config, creds=None) -> Iterator[dict]:
    from openai import OpenAI

    metrics = {"scraped": 0, "new": 0, "evaluated": 0, "fits": 0, "resumes": 0, "errors": 0}

    def m():
        return {"event": "metrics", "metrics": dict(metrics)}

    # 1. scrape ------------------------------------------------------------
    yield _stage("Scrape LinkedIn jobs", "running", "Running Apify actor (can take a minute or two)...")
    try:
        jobs = scrape_jobs(cfg)
    except Exception as e:
        yield _stage("Scrape LinkedIn jobs", "error", str(e)); return
    metrics["scraped"] = len(jobs)
    yield _stage("Scrape LinkedIn jobs", "done", f"{len(jobs)} jobs scraped")
    yield m()

    # 2. dedupe ------------------------------------------------------------
    tracker = None
    if not cfg.skip_dedupe or cfg.publish:
        try:
            tracker = JobsTracker(creds, cfg)
        except Exception as e:
            yield _stage("Remove already-processed jobs", "error", f"Cannot open sheet: {e}"); return
    if cfg.skip_dedupe:
        yield _stage("Remove already-processed jobs", "skipped", "Duplicate check disabled")
        new_jobs = jobs
    else:
        yield _stage("Remove already-processed jobs", "running", "Reading Job IDs from sheet...")
        existing = tracker.existing_job_ids()
        new_jobs = [j for j in jobs if str(j.get("id")) not in existing]
        yield _stage("Remove already-processed jobs", "done",
                     f"{len(new_jobs)} new, {len(jobs) - len(new_jobs)} already in sheet")
    metrics["new"] = len(new_jobs)
    yield m()

    if cfg.max_jobs_to_evaluate:
        new_jobs = new_jobs[: cfg.max_jobs_to_evaluate]
    if not new_jobs:
        yield _stage("Evaluate & generate resumes", "skipped", "Nothing new to process")
        yield {"event": "finished"}; return

    # 3. base resume ---------------------------------------------------------
    if cfg.resume_text_override:
        resume_text = cfg.resume_text_override
        yield _stage("Load base resume", "done", "Using pasted resume text")
    else:
        yield _stage("Load base resume", "running", "Exporting Google Doc...")
        try:
            resume_text = get_resume_text(creds, cfg)
        except Exception as e:
            yield _stage("Load base resume", "error", str(e)); return
        yield _stage("Load base resume", "done", f"{len(resume_text):,} characters")

    # 4. per-job loop -------------------------------------------------------
    openai = OpenAI(api_key=cfg.openai_api_key)
    results = [
        JobResult(id=str(j.get("id")), title=j.get("title", ""), company=j.get("companyName", ""),
                  link=j.get("link", ""), apply_url=j.get("applyUrl", ""),
                  description=j.get("descriptionText", ""))
        for j in new_jobs
    ]
    yield _stage("Evaluate & generate resumes", "running", f"0 / {len(results)} jobs")
    total = len(results)
    for i, (job, res) in enumerate(zip(new_jobs, results)):
        def emit(status=None):
            if status:
                res.status = status
            return {"event": "job", "index": i, "total": total, "job": res}

        if cfg.max_resumes and metrics["resumes"] >= cfg.max_resumes:
            res.status = "skipped"; res.reason = "Resume limit reached (not evaluated)"
            yield emit(); continue
        try:
            if not res.description.strip():
                raise ValueError("Job description is missing; cannot evaluate or tailor this job.")
            yield emit("evaluating")
            res.verdict, res.reason = is_job_a_fit(openai, cfg, resume_text, res.description)
            metrics["evaluated"] += 1
            if not res.verdict:
                yield emit("skipped"); yield m(); continue
            metrics["fits"] += 1
            yield m()

            yield emit("tailoring")
            file_id, res.latex = customize_resume(openai, cfg, res.description)

            if cfg.publish:
                yield emit("committing")
                tex_name = commit_tex_to_github(cfg, file_id, res.latex)
                yield emit("compiling")
                res.pdf = compile_pdf(cfg, tex_name)
                yield emit("uploading")
                res.resume_url = upload_pdf(creds, cfg, f"{res.company}_{res.id}.pdf", res.pdf)
                yield emit("logging")
                tracker.append({
                    "Job ID": res.id, "Job Post Link": res.link, "Resume URL": res.resume_url,
                    "Application URL": res.apply_url, "Job Title": res.title,
                    "Seniority Level": job.get("seniorityLevel"), "Posted At": job.get("postedAt"),
                    "Company Name": res.company, "Company Website": job.get("companyLinkedinUrl"),
                    "No Applicants": job.get("applicantsCount"), "Job Description": job.get("descriptionHtml"),
                })
            metrics["resumes"] += 1
            yield emit("done"); yield m()
        except Exception as e:
            res.error = str(e); metrics["errors"] += 1
            yield emit("error"); yield m()
        yield _stage("Evaluate & generate resumes", "running", f"{i + 1} / {total} jobs")

    yield _stage("Evaluate & generate resumes", "done",
                 f"{metrics['fits']} fits, {metrics['resumes']} resumes, {metrics['errors']} errors")
    yield {"event": "finished"}
