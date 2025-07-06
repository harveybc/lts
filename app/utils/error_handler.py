#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
error_handler.py

Provides a centralized error handling utility for the LTS application.
"""

import re

class ErrorHandler:
    """
    Handles errors by sanitizing sensitive information from error messages.
    """
    def process_error(self, error: Exception) -> dict:
        """
        Process an exception to create a sanitized error report.

        :param error: The exception that was caught.
        :return: A dictionary containing the error type and sanitized message.
        """
        error_type = type(error).__name__
        sanitized_message = self._sanitize_message(str(error))
        
        return {
            "type": error_type,
            "message": sanitized_message
        }

    def _sanitize_message(self, msg: str) -> str:
        """
        Sanitize error messages to remove sensitive information.

        :param msg: The error message string to sanitize.
        :return: Sanitized error message string.
        """
        # Remove common sensitive patterns (e.g., password, secret, token)
        msg = re.sub(r'(password|secret|token)\s*=\s*[^,;\s]+', r'\1=***', msg, flags=re.IGNORECASE)
        # Remove any obvious sensitive info
        msg = re.sub(r'\b\d{4,}\b', '****', msg)  # Mask long numbers
        return msg
