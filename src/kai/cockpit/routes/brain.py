"""Brain routes: /brain — create the workspace, edit the operator instruction,
upload/paste/crawl documents, list + delete them.
"""

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from kai.cockpit.app import templates
from kai.cockpit.auth import require_user
from kai.cockpit.brains import BrainsService
from kai.cockpit.db import get_db
from kai.cockpit.flash import flash
from kai.cockpit.models import User
from kai.cockpit.service_health import check_crawler_health

router = APIRouter()


@router.get("/brain")
async def brains_page(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    brain = svc.get_brain(user)
    docs = []
    docs_error = None
    if brain is not None:
        try:
            docs = await svc.list_docs(user)
        except Exception as exc:
            docs_error = str(exc)
    any_processing = any(not d.is_terminal for d in docs)
    flash = request.session.pop("flash", None)
    # Gate the "Add a website" form on crawler availability — crawl4ai is the
    # only source that needs an external container; uploads/paste still work.
    crawler_health = await check_crawler_health()
    crawler_ok = crawler_health.ok if crawler_health else True
    return templates.TemplateResponse(
        request,
        "brain.html",
        {
            "user": user,
            "brain": brain,
            "docs": docs,
            "docs_error": docs_error,
            "any_processing": any_processing,
            "crawler_ok": crawler_ok,
            "crawler_detail": crawler_health.detail if crawler_health else "",
            "flash": flash,
        },
    )


@router.post("/brain/create")
async def brains_create(
    request: Request,
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    try:
        svc.create_brain(user)
        flash(request, "success", "Brain created.")
    except Exception as exc:
        flash(request, "error", f"Could not create Brain: {exc}")
    return RedirectResponse("/brain", status_code=302)


@router.post("/brain/instruction")
async def brains_update_instruction(
    request: Request,
    instruction: str = Form(""),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    try:
        svc.update_instruction(user, instruction=instruction)
        # update_instruction flags running deployments needs_restart=True;
        # tell the operator so they know to restart for the change to take
        # effect on the live bot.
        flash(request, "info", "Brain instructions saved. Restart your bots to apply.")
    except ValueError as exc:
        flash(request, "warn", str(exc))
    except Exception as exc:
        flash(request, "error", f"Could not save instructions: {exc}")
    return RedirectResponse("/brain", status_code=302)


@router.post("/brain/upload")
async def brains_upload(
    request: Request,
    file: UploadFile = File(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    try:
        if not file.filename:
            raise ValueError("No file selected.")
        await svc.ingest_file(user, filename=file.filename, file=file.file)
        flash(request, "info", f"Uploaded {file.filename}. kAI is adding it to the Brain.")
    except ValueError as exc:
        flash(request, "warn", str(exc))
    except Exception as exc:
        flash(request, "error", f"Upload failed: {exc}")
    return RedirectResponse("/brain", status_code=302)


@router.post("/brain/ingest-url")
async def brains_ingest_url(
    request: Request,
    url: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    url = url.strip()
    try:
        if not url:
            raise ValueError("URL is required.")
        crawler = await check_crawler_health()
        if crawler is not None and not crawler.ok:
            raise ValueError(
                f"The Crawler service is unavailable ({crawler.detail}). "
                "Check the Crawler container and try again."
            )
        result = await svc.ingest_url(user, url=url)
        flash(request, "info", f"Added {url}. kAI is saving it to the Brain. ({result.message})")
    except ValueError as exc:
        flash(request, "warn", str(exc))
    except Exception as exc:
        flash(request, "error", f"Could not add website: {exc}")
    return RedirectResponse("/brain", status_code=302)


@router.post("/brain/ingest-text")
async def brains_ingest_text(
    request: Request,
    name: str = Form(...),
    text: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    name = name.strip()
    text = text.strip()
    try:
        if not name:
            raise ValueError("Name is required for pasted text.")
        if not text:
            raise ValueError("Text is required.")
        await svc.ingest_text(user, name=name, text=text)
        flash(request, "info", f"Added {name}. kAI is saving it to the Brain.")
    except ValueError as exc:
        flash(request, "warn", str(exc))
    except Exception as exc:
        flash(request, "error", f"Could not add text: {exc}")
    return RedirectResponse("/brain", status_code=302)


@router.post("/brain/documents/delete")
async def brains_delete_document(
    request: Request,
    doc_id: str = Form(...),
    user: User = Depends(require_user),
    db: Session = Depends(get_db),
):
    svc = BrainsService(db)
    try:
        await svc.delete_doc(user, doc_id=doc_id)
        flash(request, "success", "Document deleted.")
    except ValueError as exc:
        flash(request, "warn", str(exc))
    except Exception as exc:
        flash(request, "error", f"Could not delete document: {exc}")
    return RedirectResponse("/brain", status_code=302)
