from __future__ import annotations


class AzwiError(Exception):
    exit_code = 6


class UsageError(AzwiError):
    exit_code = 2


class ConfigError(AzwiError):
    exit_code = 3


class AuthError(AzwiError):
    exit_code = 4


class NotFoundError(AzwiError):
    exit_code = 5


class ApiError(AzwiError):
    exit_code = 6


class ThrottledError(ApiError):
    exit_code = 7
