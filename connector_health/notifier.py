import smtplib
import ssl


class MailError(Exception):
    pass


def send_email(message, config):
    try:
        with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls(context=ssl.create_default_context())
            smtp.ehlo()
            smtp.login(config.smtp_user, config.smtp_password)
            refused = smtp.send_message(
                message, from_addr=config.smtp_user, to_addrs=list(config.recipients)
            )
            if refused:
                raise MailError("recipient_refused")
    except (smtplib.SMTPException, OSError):
        raise MailError("smtp_failed") from None
