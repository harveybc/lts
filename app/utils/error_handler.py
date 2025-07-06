import traceback

class ErrorHandler:
    def process_error(self, error):
        return {
            "type": error.__class__.__name__,
            "message": str(error),
            "traceback": traceback.format_exc()
        }
