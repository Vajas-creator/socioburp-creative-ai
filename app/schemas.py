from pydantic import BaseModel


class IncomingMessage(BaseModel):
    sender: str  # phone number, international format, no '+'
    type: str  # "text" | "image" | "button"
    text: str | None = None
    media_id: str | None = None
    button_id: str | None = None
