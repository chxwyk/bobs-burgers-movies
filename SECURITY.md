# Security Notes

- Keep `DISCORD_TOKEN` only in Railway variables or a local `.env` file.
- Never upload `.env` to GitHub. The included `.gitignore` blocks it.
- Keep the movie ticket category and transcript channel private.
- Give the bot only the permissions listed in `README.md`.
- Payment instructions may contain a payment tag, address, or link, but must never
  contain passwords, bank credentials, full card numbers, security codes, seed
  phrases, private keys, or customer account credentials.
- Staff must verify payment manually before delivering tickets.
- QR ticket codes should only be posted in the private customer ticket.
- Review transcript-channel access regularly because transcripts contain customer
  order details and message attachments.
- Use the bot only for authorized purchases that comply with applicable laws and
  theater terms.
