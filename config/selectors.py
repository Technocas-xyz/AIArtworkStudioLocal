# Per-workflow ChatGPT project. An empty value falls back to PROJECT_URL_DEFAULT
# (a plain new chat, no project). Left empty so jobs run directly in a new chat
# on whichever ChatGPT account is signed in; a project URL only works for the
# account that owns that project. To route a workflow into a project again, paste
# its URL (e.g. "https://chatgpt.com/g/g-p-<id>/project") below.
PROJECT_URLS = {
    "text":    "",   # DTF - Text Designs
    "mockup":  "",   # DTF - Artwork Extraction
    "artwork": "",   # DTF - Artwork Generation
    "custom":  "",   # DTF - Custom Operations
}
PROJECT_URL_DEFAULT = "https://chatgpt.com"

# The editable chat title control (pencil / "Rename" in the conversation menu).
# Used best-effort to name a chat "<client> - <task_id>"; skipped silently if
# ChatGPT's markup differs, since renaming is cosmetic.
CHAT_TITLE_INPUT = "input[aria-label='Chat title'], input[name='conversation-title']"

FILE_INPUT    = "input[data-testid='upload-photos-input']"
PROMPT_BOX    = "#prompt-textarea"
STOP_BUTTON   = "[data-testid='stop-button']"
IMAGE_LOADER  = "[data-testid='image-gen-loading-state']"
IMAGE_OVERLAY = "[data-testid='image-gen-overlay-actions']"
NEW_CHAT      = "[data-testid='create-new-chat-button']"

GENERATED_IMG = "img[src*='backend-api/estuary/content']"

# Upload preview in the composer. Older ChatGPT used a local blob: URL; current
# ChatGPT shows the uploaded file from backend-api/estuary/content instead.
COMPOSER_THUMBNAIL = "img[src^='blob:'], img[src*='backend-api/estuary/content']"
SEND_BUTTON        = "[data-testid='send-button']"

CONVERSATION_TURN = "section[data-testid^='conversation-turn-']"
