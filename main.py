#!/usr/bin/env python3
"""CLI runner (no UI). For the visual version run:  streamlit run app.py"""
import logging
import os

from dotenv import load_dotenv

from pipeline import Config, google_credentials, run_pipeline

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("resume_creator")


def main() -> None:
    cfg = Config(
        apify_token=os.environ["APIFY_TOKEN"],
        openai_api_key=os.environ["OPENAI_API_KEY"],
        github_token=os.getenv("GITHUB_TOKEN", ""),
    )
    creds = google_credentials() if cfg.needs_google() else None
    for ev in run_pipeline(cfg, creds):
        if ev["event"] == "stage":
            log.info("[%s] %s %s", ev["name"], ev["state"], ev["detail"])
        elif ev["event"] == "job" and ev["job"].status in ("done", "skipped", "error"):
            j = ev["job"]
            log.info("%d/%d %s @ %s -> %s %s", ev["index"] + 1, ev["total"], j.title, j.company,
                     j.status, j.resume_url or j.reason or j.error)


if __name__ == "__main__":
    main()
