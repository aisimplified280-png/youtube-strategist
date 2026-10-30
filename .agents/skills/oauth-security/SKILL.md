---
name: oauth-security
description: >-
  Manage secure OAuth authentication, token encryption, scope isolation, and secret handling
  for YouTube API and Google Drive with strict owner access.
  Use when modifying OAuth flows, token encryption, credential handling, or auth redirects.
---

# OAuth Security

Protocols for safeguarding owner credentials and token lifetimes.

## Security Mandates

1. **Owner-Only Authentication**:
   - Verify owner email strictly against authorized administrator configuration.
   - Unauthorized accounts must be rejected with HTTP 403 / Access Denied.

2. **Scope Isolation**:
   - YouTube OAuth:
     - `https://www.googleapis.com/auth/yt-analytics.readonly`
     - `https://www.googleapis.com/auth/youtube.force-ssl`
   - Memory OAuth:
     - `https://www.googleapis.com/auth/drive.file` (scoped strictly to files created by the app).
   - Never combine or conflate YouTube tokens with Google Drive tokens.

3. **Cryptographic Token Vault**:
   - Persist tokens encrypted with PBKDF2 (100k+ iterations) and AES-GCM or Fernet using a strong secret key.
   - Never write raw refresh tokens or client secrets to logs, URLs, or browser local storage.

4. **PKCE and State Verification**:
   - Enforce CSRF protection on callback with cryptographically random HMAC-bound state parameters.
   - Rederive PKCE code verifier deterministically or store securely in encrypted session state.

## Acceptance Criteria

- Tokens encrypted at rest in local storage.
- Callback verifies state parameter integrity before token exchange.
- Zero secrets or tokens emitted in application logs, error traces, or URLs.
