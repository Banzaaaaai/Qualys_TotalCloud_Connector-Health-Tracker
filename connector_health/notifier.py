import smtplib
import ssl


class MailError(Exception):
    pass


def send_email(message, config):
    try:
        with smtplib.SMTP_SSL(
            "smtp.gmail.com", 465, timeout=30, context=ssl.create_default_context()
        ) as smtp:
            smtp.login(config.gmail_user, config.gmail_password)
            refused = smtp.send_message(
                message, from_addr=config.gmail_user, to_addrs=list(config.recipients)
            )
            if refused:
                raise MailError("recipient_refused")
    except (smtplib.SMTPException, OSError):
        raise MailError("smtp_failed") from None
