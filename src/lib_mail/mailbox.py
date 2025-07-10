import os
import re
import imaplib
import chardet
import logging
from pathlib import Path
from bs4 import BeautifulSoup
from lib_invoice import Invoice
from lib_utilys import read_json
from lib_idoc.invoice import IDOC
from email import message_from_string
from email.header import decode_header
from typing import Any, Type, Iterator, Tuple

logger = logging.getLogger(__name__)

class Mailbox:
    def __init__(self, inbox: str = 'INBOX'):
        self.inbox = os.getenv('IMAP_INBOX', inbox)
        self.server = os.getenv('IMAP_SERVER')
        self.port = os.getenv('IMAP_PORT')
        self.email = os.getenv('IMAP_EMAIL')
        self.password = os.getenv('IMAP_PASSWORD')
        self.imap_server = None
        self.uid = None
        self._connect(self.server, self.port, self.email, self.password)

    def __del__(self):
        if hasattr(self, 'imap_server') and self.imap_server:
            self._disconnect()

    def _connect(self, server: str, port: str, email: str, password: str):
        """Connects to the email server."""
        try:
            self.imap_server = imaplib.IMAP4_SSL(server, int(port))
            self.imap_server.login(email, password)
            self.imap_server.select(self.inbox)
        except Exception as e:
            logger.exception("Error connecting to email server")

    def _disconnect(self):
        """Disconnects from the email server."""
        self.imap_server.logout()

    def initialize_uid(self, slider_value: int):
        """Initializes the UID of the last email."""
        try:
            _, uids = self.imap_server.uid('search', None, 'ALL')
            if uids[0]:
                self.uid = max(int(uid.decode('utf-8')) for uid in uids[0].split()) - 1 - slider_value
                logger.info("Initialized UID: %s", self.uid)
            else:
                logger.info("No emails found.")
        except Exception as e:
            logger.exception("Error initializing UID")

    def list_inboxes(self):
        """Lists all available mailboxes (folders)."""
        try:
            status, mailboxes = self.imap_server.list()

            if status == 'OK':
                logger.info("Available mailboxes:")
                for mailbox in mailboxes:
                    logger.info(mailbox.decode()) 
        except Exception as e:
            logger.exception("Error listing mailboxes")

    def list_uids(self) -> list[str]:
        """Scans the email inbox for new emails."""
        try:
            search_criteria = f"UID {int(self.uid) + 1}:*"
            logger.info(f"Searching for emails with criteria: {search_criteria}")
            _, uids = self.imap_server.uid('search', None, search_criteria)
            uids = [uid.decode('utf-8') for uid in uids[0].split()]
            uids = [uid for uid in uids if int(uid) > int(self.uid)]
            return uids
        except Exception as e:
            logger.exception("Error listing UIDs")

    def create_invoice_and_idoc(
            self, 
            uids: list[str],
            invoice_cls: Type[Invoice], 
            idoc_cls: Type[IDOC],
            startseg_path: Path,
            dynseg_path: Path,
            endseg_path: Path
        ) -> Iterator[Tuple[Invoice, IDOC]]:
        """Yield (Invoice, IDOC) instances for each pdf in each new mail."""
        for uid in uids:
            message, adress, business, subject, text, pdfs = self.configure_uid_specific_data(uid)
            for pdf in pdfs:
                invoice = invoice_cls(uid, adress, message, business, subject, text, pdf)
                idoc = idoc_cls(startseg_path, dynseg_path, endseg_path)
                yield invoice, idoc

    def should_process(self, crit_path: Path, invoice: Invoice) -> bool:
        """Determines whether an email should be processed"""
        criteria = read_json(crit_path)
        return invoice.business in criteria and re.search(criteria[invoice.business], invoice.subject)

    def delete_email(self, uid: str):
        """Deletes an email from the inbox."""
        try:
            self.imap_server.uid('STORE', uid , '+FLAGS', '(\Deleted)')
            self.imap_server.expunge()
        except Exception as e:
            logger.exception("Error deleting email")
            
    def flag_email(self, uid: str):
        """Flags an email in the inbox."""
        try:
            self.imap_server.uid('STORE', uid , '+FLAGS', '(\Flagged)')
        except Exception as e:
            logger.exception("Error flagging email")

    def extract_minimal_metadata(self, uids: list[str]) -> list[tuple[str, str, str]]:
        """Fetches the minimal metadata of an email."""
        try:
            metadata = []
            for uid in uids:
                _, data = self.imap_server.uid('fetch', uid, '(RFC822)')
                charset = chardet.detect(data[0][1])['encoding']
                raw_data= data[0][1].decode(charset)
                message = message_from_string(raw_data)
                adress = message['From']
                business = self.extract_business_(adress)
                decoded_fragments = decode_header(message['Subject'])
                subject = ''.join([
                fragment.decode(charset or 'utf-8') if isinstance(fragment, bytes) else fragment
                for fragment, charset in decoded_fragments
                ])
                metadata.append((uid, business, subject))
                
            return metadata
        except Exception as e:
            logger.error(f"Error extracting minimal metadata: {e}")
            return []

    def extract_business_(self, email_adress: str) -> str | None:
        """Extracts the business name from an email address."""
        match = re.search(r'[\w\.-]+@[\w\.-]+', email_adress)
        extracted_email = match.group(0) if match else email_adress
        email_parts = extracted_email.split("@")
        if len(email_parts) == 2:
            domain_parts = email_parts[1].split(".")
            if len(domain_parts) > 1:
                business_name = domain_parts[0]
                return business_name
        return None

    def extract_all_pdfs(self, parts: list) -> list[bytes]:
            try:
                parts = [part for part in parts if ".pdf" in str(part.get_filename()).lower()]
                pdfs = [payload.get_payload(decode=True) for payload in parts]
                return pdfs
            except Exception as e:
                logger.error(f"Error extracting all pdfs: {e}")

    def extract_text_(self, parts: list) -> str:
        """Extracts the plain text from the email."""
        try:
            parts = [part for part in parts if part.get_content_type() == 'text/plain' or part.get_content_type() == 'text/html']
            plain_text = ''
            for part in parts:
                charset = part.get_content_charset() or 'utf-8'
                plain_text += part.get_payload(decode=True).decode(charset)
            soup = BeautifulSoup(plain_text, 'html.parser')
            plain_text = soup.get_text()
            return plain_text
        except Exception as e:
            logger.error(f"Error extracting plain text: {e}")

    def configure_uid_specific_data(self, uid: str) -> Any:
        """Fetches the email content."""
        try:
            _, data = self.imap_server.uid('fetch', uid, '(RFC822)')
            charset = chardet.detect(data[0][1])['encoding']
            raw_data= data[0][1].decode(charset)
            message = message_from_string(raw_data)
            adress = message['From']
            business = self.extract_business_(adress)
            decoded_fragments = decode_header(message['Subject'])
            subject = ''.join([
            fragment.decode(charset or 'utf-8') if isinstance(fragment, bytes) else fragment
            for fragment, charset in decoded_fragments
            ])
            parts = list(message.walk())
            text = self.extract_text_(parts)
            pdfs = self.extract_all_pdfs(parts)
            return message, adress, business, subject, text, pdfs
        except Exception as e:
            logger.exception("Error configuring UID specific data")
            return None, None, None, None, None, []
