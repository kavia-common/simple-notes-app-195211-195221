from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Response, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field, field_validator


def _utc_now() -> datetime:
    """Return the current UTC time as an aware datetime."""
    return datetime.now(timezone.utc)


class NoteBase(BaseModel):
    """Common fields shared by note create/update payloads."""

    title: str = Field(..., description="Short title for the note.")
    content: str = Field("", description="Full note content (may be empty).")

    @field_validator("title")
    @classmethod
    def validate_title_non_empty(cls, v: str) -> str:
        """Validate that the title is not empty after trimming whitespace."""
        if v is None or not v.strip():
            raise ValueError("title must be non-empty")
        return v.strip()


class NoteCreate(NoteBase):
    """Payload for creating a note."""


class NoteUpdate(BaseModel):
    """Payload for updating a note. Fields are optional; at least one must be provided."""

    title: Optional[str] = Field(None, description="Updated title for the note.")
    content: Optional[str] = Field(None, description="Updated content for the note.")

    @field_validator("title")
    @classmethod
    def validate_title_if_present(cls, v: Optional[str]) -> Optional[str]:
        """Validate title if provided; must be non-empty after trimming."""
        if v is None:
            return v
        if not v.strip():
            raise ValueError("title must be non-empty")
        return v.strip()


class Note(NoteBase):
    """Note representation returned by the API."""

    id: str = Field(..., description="Unique note id (UUID).")
    created_at: datetime = Field(..., description="Timestamp when the note was created (UTC).")
    updated_at: datetime = Field(..., description="Timestamp when the note was last updated (UTC).")


class NotesStore:
    """Very small JSON-file-backed store (falls back to in-memory if file ops fail).

    Data is kept in memory and flushed to disk on each mutation. This is sufficient for a
    simple demo app with a single process.
    """

    def __init__(self, file_path: str) -> None:
        self._file_path = file_path
        self._lock = threading.Lock()
        self._notes: Dict[str, Note] = {}
        self._load()

    def _load(self) -> None:
        """Load notes from disk if file exists."""
        try:
            if not os.path.exists(self._file_path):
                return
            with open(self._file_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            notes = {}
            for n in raw.get("notes", []):
                note = Note.model_validate(n)
                notes[note.id] = note
            self._notes = notes
        except Exception:
            # If file is corrupted or unreadable, keep in-memory empty store.
            self._notes = {}

    def _persist(self) -> None:
        """Persist notes to disk (best effort)."""
        os.makedirs(os.path.dirname(self._file_path), exist_ok=True)
        payload = {"notes": [n.model_dump(mode="json") for n in self.list_notes()]}
        with open(self._file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def list_notes(self) -> List[Note]:
        """Return notes sorted by updated_at descending."""
        return sorted(self._notes.values(), key=lambda n: n.updated_at, reverse=True)

    def get_note(self, note_id: str) -> Optional[Note]:
        """Return a note by id, or None."""
        return self._notes.get(note_id)

    def create_note(self, payload: NoteCreate) -> Note:
        """Create and persist a new note."""
        with self._lock:
            now = _utc_now()
            note = Note(
                id=str(uuid4()),
                title=payload.title,
                content=payload.content,
                created_at=now,
                updated_at=now,
            )
            self._notes[note.id] = note
            try:
                self._persist()
            except Exception:
                # best effort persistence
                pass
            return note

    def update_note(self, note_id: str, payload: NoteUpdate) -> Note:
        """Update and persist an existing note."""
        with self._lock:
            existing = self._notes.get(note_id)
            if existing is None:
                raise KeyError(note_id)

            if payload.title is None and payload.content is None:
                # Keep strict about request intent
                raise ValueError("At least one field (title or content) must be provided.")

            updated = existing.model_copy(
                update={
                    **({"title": payload.title} if payload.title is not None else {}),
                    **({"content": payload.content} if payload.content is not None else {}),
                    "updated_at": _utc_now(),
                }
            )
            self._notes[note_id] = updated
            try:
                self._persist()
            except Exception:
                pass
            return updated

    def delete_note(self, note_id: str) -> bool:
        """Delete a note. Returns True if deleted."""
        with self._lock:
            existed = note_id in self._notes
            if existed:
                del self._notes[note_id]
                try:
                    self._persist()
                except Exception:
                    pass
            return existed


openapi_tags = [
    {"name": "Health", "description": "Service health checks."},
    {"name": "Notes", "description": "Create, read, update, and delete notes."},
]

app = FastAPI(
    title="Simple Notes API",
    description=(
        "Backend API for a simple notes app.\n\n"
        "Provides CRUD endpoints under /notes for a React frontend."
    ),
    version="1.0.0",
    openapi_tags=openapi_tags,
)

# Restrict CORS to the local frontend dev server as requested.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

_store = NotesStore(file_path=os.path.join(os.path.dirname(__file__), "..", "..", "data", "notes.json"))


@app.get("/", tags=["Health"], summary="Health check", operation_id="health_check")
# PUBLIC_INTERFACE
def health_check():
    """Health check endpoint.

    Returns:
        JSON with a status message.
    """
    return {"message": "Healthy"}


@app.get(
    "/notes",
    response_model=List[Note],
    tags=["Notes"],
    summary="List notes",
    operation_id="list_notes",
)
# PUBLIC_INTERFACE
def list_notes() -> List[Note]:
    """List all notes (most recently updated first).

    Returns:
        A JSON array of notes.
    """
    return _store.list_notes()


@app.post(
    "/notes",
    response_model=Note,
    status_code=status.HTTP_201_CREATED,
    tags=["Notes"],
    summary="Create note",
    operation_id="create_note",
)
# PUBLIC_INTERFACE
def create_note(payload: NoteCreate) -> Note:
    """Create a new note.

    Args:
        payload: NoteCreate payload containing title and optional content.

    Returns:
        The created Note.

    Raises:
        HTTPException(422): If validation fails (e.g., empty title).
    """
    return _store.create_note(payload)


@app.get(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Get note by id",
    operation_id="get_note",
)
# PUBLIC_INTERFACE
def get_note(note_id: str) -> Note:
    """Get a single note by its id.

    Args:
        note_id: Note id (UUID string).

    Returns:
        The requested Note.

    Raises:
        HTTPException(404): If note is not found.
    """
    note = _store.get_note(note_id)
    if note is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return note


@app.put(
    "/notes/{note_id}",
    response_model=Note,
    tags=["Notes"],
    summary="Update note",
    operation_id="update_note",
)
# PUBLIC_INTERFACE
def update_note(note_id: str, payload: NoteUpdate) -> Note:
    """Update an existing note.

    Args:
        note_id: Note id (UUID string).
        payload: NoteUpdate payload. Provide title and/or content.

    Returns:
        The updated Note.

    Raises:
        HTTPException(404): If note is not found.
        HTTPException(400): If request contains no updatable fields.
        HTTPException(422): If validation fails (e.g., empty title).
    """
    try:
        return _store.update_note(note_id, payload)
    except KeyError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    except ValueError as e:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))


@app.delete(
    "/notes/{note_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    tags=["Notes"],
    summary="Delete note",
    operation_id="delete_note",
)
# PUBLIC_INTERFACE
def delete_note(note_id: str) -> Response:
    """Delete a note by id.

    Args:
        note_id: Note id (UUID string).

    Returns:
        Empty response with 204 on success.

    Raises:
        HTTPException(404): If note is not found.
    """
    deleted = _store.delete_note(note_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Note not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)
