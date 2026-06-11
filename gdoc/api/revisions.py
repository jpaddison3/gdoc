"""Drive API revisions wrappers (milestone revision history).

Google Docs' rich "Version history" UI has no public API, but the Drive
``revisions`` collection returns milestone revisions for native Docs,
and each milestone is exportable via authenticated ``exportLinks``.
Revision ids are sparse and non-``keepForever`` revisions are pruned by
Google over time (~weeks).
"""

from googleapiclient.errors import HttpError

from gdoc.api import get_drive_service
from gdoc.util import AuthError, GdocError

_REVISION_FIELDS = (
    "id, modifiedTime, keepForever, "
    "lastModifyingUser(displayName, emailAddress), exportLinks"
)


def _translate_http_error(e: HttpError, file_id: str) -> None:
    """Translate HttpError for revisions operations."""
    status = int(e.resp.status)
    if status == 401:
        raise AuthError("Authentication expired. Run `gdoc auth`.")
    if status == 403:
        raise GdocError(f"Permission denied: {file_id}")
    if status == 404:
        raise GdocError(f"Document not found: {file_id}")
    raise GdocError(f"API error ({status}): {e.reason}")


def _pruned_error(revision_id: str) -> GdocError:
    return GdocError(
        f"revision not found: {revision_id} (it may have been pruned — "
        "Google drops non-pinned revisions over time). "
        "Run `gdoc revisions DOC` to see retained revisions.",
        exit_code=3,
    )


def list_revisions(file_id: str) -> list[dict]:
    """List retained revisions for a file, oldest first, auto-paginating.

    Returns revision dicts with id, modifiedTime, keepForever,
    lastModifyingUser, and exportLinks.
    """
    try:
        service = get_drive_service()
        revisions: list[dict] = []
        page_token = None

        while True:
            response = (
                service.revisions()
                .list(
                    fileId=file_id,
                    pageSize=1000,
                    fields=f"nextPageToken, revisions({_REVISION_FIELDS})",
                    pageToken=page_token,
                )
                .execute()
            )
            revisions.extend(response.get("revisions", []))
            page_token = response.get("nextPageToken")
            if page_token is None:
                break

        return revisions
    except HttpError as e:
        _translate_http_error(e, file_id)


def export_revision(
    file_id: str,
    revision_id: str,
    mime_type: str = "text/markdown",
    export_links: dict | None = None,
) -> str:
    """Export one revision's content via its exportLinks.

    The Drive API has no export endpoint for revisions of native Docs;
    each revision instead carries authenticated exportLinks URLs. Falls
    back to text/plain when the requested mime type has no link.

    Args:
        file_id: The document ID.
        revision_id: The revision ID to export.
        mime_type: Preferred export format.
        export_links: exportLinks dict from a prior revisions.list call;
            when omitted, fetched via revisions.get.
    """
    if export_links is None:
        try:
            service = get_drive_service()
            result = (
                service.revisions()
                .get(
                    fileId=file_id,
                    revisionId=revision_id,
                    fields="exportLinks",
                )
                .execute()
            )
            export_links = result.get("exportLinks", {})
        except HttpError as e:
            if int(e.resp.status) == 404:
                raise _pruned_error(revision_id)
            _translate_http_error(e, file_id)

    url = export_links.get(mime_type) or export_links.get("text/plain")
    if not url:
        raise GdocError(
            f"revision {revision_id} has no {mime_type} export link"
        )

    from google.auth.transport.requests import AuthorizedSession

    from gdoc.auth import get_credentials

    session = AuthorizedSession(get_credentials())
    response = session.get(url)
    if response.status_code == 404:
        raise _pruned_error(revision_id)
    if response.status_code in (401, 403):
        raise AuthError("Authentication expired. Run `gdoc auth`.")
    if response.status_code != 200:
        raise GdocError(
            f"export failed for revision {revision_id} "
            f"(HTTP {response.status_code})"
        )
    response.encoding = "utf-8"
    return response.text
