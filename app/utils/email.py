"""
Transactional email stub.

FUSE LMS sends account-activation links, password resets, and notification
emails through Account C's (client-facing Google Workspace) transactional
email service. Wire this up to that provider — swap the body of send_email()
for an actual SMTP/API call once Account C's project is provisioned.
Left as a stub + print so the rest of the app is fully runnable/testable
without live credentials.
"""


def send_email(to: str, subject: str, body: str) -> None:
    print(f"--- EMAIL to {to} ---\nSubject: {subject}\n\n{body}\n---------------------")
