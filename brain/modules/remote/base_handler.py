class BaseRemoteHandler:
    """Common interface for remote (chat-based) control channels."""

    def send_message(self, chat_id, text):
        pass

    def get_updates(self, offset=None):
        pass
