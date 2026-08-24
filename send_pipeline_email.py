"""Send the cloud pipeline result through Gmail SMTP without exposing secrets."""

from __future__ import annotations

import os
import smtplib
import zipfile
from email.message import EmailMessage
from pathlib import Path


OUTPUT_DIRS = (Path("output_selector"), Path("output_integrated_stock"))


def build_output_zip(run_date: str) -> tuple[Path, list[str]]:
    zip_path = Path(f"pipeline_outputs_{run_date}.zip")
    included: list[str] = []
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for directory in OUTPUT_DIRS:
            if not directory.exists():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    arcname = path.as_posix()
                    archive.write(path, arcname)
                    included.append(arcname)
        log_path = Path("pipeline.log")
        if log_path.exists():
            archive.write(log_path, log_path.name)
            included.append(log_path.name)
    return zip_path, included


def main() -> None:
    username = os.environ["SMTP_USERNAME"]
    password = os.environ["SMTP_APP_PASSWORD"].replace(" ", "")
    recipient = os.environ["EMAIL_TO"]
    run_date = os.getenv("RUN_DATE", "unknown-date")
    exit_code = int(os.getenv("PIPELINE_EXIT_CODE", "1"))
    status = "SUCCESS" if exit_code == 0 else "FAILED"
    run_url = (
        f"{os.getenv('GITHUB_SERVER_URL', 'https://github.com')}/"
        f"{os.getenv('GITHUB_REPOSITORY', '')}/actions/runs/"
        f"{os.getenv('GITHUB_RUN_ID', '')}"
    )

    zip_path, included = build_output_zip(run_date)
    message = EmailMessage()
    message["Subject"] = f"[Fubon Pipeline] {run_date} {status}"
    message["From"] = username
    message["To"] = recipient
    message.set_content(
        "\n".join(
            [
                f"Pipeline status: {status}",
                f"Run date: {run_date}",
                f"Output files: {len(included)}",
                f"Google Drive: Complete_Pullback_Fubon_Pipeline/runs/{run_date}",
                f"GitHub run: {run_url}",
                "",
                "The attached ZIP contains report outputs and pipeline.log only.",
                "Strategy ledger and credentials are not attached.",
            ]
        )
    )

    # Gmail's total message limit is about 25 MB; leave room for MIME/base64 overhead.
    if zip_path.stat().st_size <= 18 * 1024 * 1024:
        message.add_attachment(
            zip_path.read_bytes(),
            maintype="application",
            subtype="zip",
            filename=zip_path.name,
        )
    else:
        message.set_content(message.get_content() + "\nAttachment omitted because it exceeds 18 MB.\n")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as smtp:
        smtp.login(username, password)
        smtp.send_message(message)
    print(f"Pipeline email sent to {recipient}")


if __name__ == "__main__":
    main()
