"""
Database operations for the price comparison pipeline.

Supports optional SSH tunneling for secure database access through bastion hosts.
Enable by setting SSH_ENABLED=true and configuring SSH_* environment variables.
"""

import atexit
import logging
import os
import socket
import threading
from dataclasses import dataclass
from datetime import datetime
from contextlib import closing
from pathlib import Path
from typing import Any

import mysql.connector
from dotenv import load_dotenv

from scraper.models import OfferPart

try:
    from scraper.db_schema import (
        FETCH_ITEMS,
        UPDATE_RRP,
        VERIFY_OFFER,
        FETCH_RECENT_OFFERS,
        FETCH_CURRENT_RRP,
        COL_ID,
        COL_NAME,
        COL_PRICE,
        COL_CATEGORY,
        COL_OFFER,
        COL_RRP,
        COL_COUNT,
        COL_LATEST,
        FRUIT_CATEGORY,
        VEG_CATEGORY,
    )
except ImportError:
    raise ImportError(
        "Database schema not found. Copy scraper/db_schema.example.py "
        "to scraper/db_schema.py and customise for your database."
    )

load_dotenv()


@dataclass
class OfferInfo:
    """Summary information about an offer."""

    offer_id: int
    item_count: int
    latest_updated: datetime | None

logger = logging.getLogger(__name__)


class _ParamikoTunnel:
    """SSH tunnel using paramiko directly, replacing sshtunnel dependency."""

    def __init__(
        self,
        ssh_host: str,
        ssh_port: int,
        ssh_user: str,
        ssh_key_path: str,
        remote_host: str,
        remote_port: int,
        ssh_key_passphrase: str | None = None,
    ):
        import paramiko

        self._remote_host = remote_host
        self._remote_port = remote_port
        self._shutdown_event = threading.Event()

        # Connect SSH client
        self._client = paramiko.SSHClient()
        self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

        connect_kwargs: dict = {
            "hostname": ssh_host,
            "port": ssh_port,
            "username": ssh_user,
            "key_filename": ssh_key_path,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if ssh_key_passphrase:
            connect_kwargs["passphrase"] = ssh_key_passphrase

        self._client.connect(**connect_kwargs)
        self._transport = self._client.get_transport()

        # Bind local server socket on an auto-assigned port
        self._server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._server_sock.bind(("127.0.0.1", 0))
        self._server_sock.listen(5)
        self._server_sock.settimeout(1.0)
        self._local_bind_port = self._server_sock.getsockname()[1]

        # Start accept loop in a daemon thread
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()

    @property
    def local_bind_port(self) -> int:
        return self._local_bind_port

    def _accept_loop(self):
        """Accept local connections and forward them through the SSH tunnel."""
        while not self._shutdown_event.is_set():
            try:
                client_sock, _ = self._server_sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break

            try:
                channel = self._transport.open_channel(
                    "direct-tcpip",
                    (self._remote_host, self._remote_port),
                    client_sock.getpeername(),
                )
            except Exception as e:
                logger.warning(f"SSH channel open failed: {e}")
                client_sock.close()
                continue

            if channel is None:
                logger.warning("SSH channel open returned None")
                client_sock.close()
                continue

            # Spawn two daemon threads to forward data in each direction
            t1 = threading.Thread(
                target=self._forward_data, args=(client_sock, channel), daemon=True
            )
            t2 = threading.Thread(
                target=self._forward_data, args=(channel, client_sock), daemon=True
            )
            t1.start()
            t2.start()

    def _forward_data(self, src, dst):
        """Forward data from src to dst until closed or shutdown."""
        try:
            src.settimeout(2.0)
        except Exception:
            pass
        try:
            while not self._shutdown_event.is_set():
                try:
                    data = src.recv(4096)
                    if not data:
                        break
                    dst.sendall(data)
                except socket.timeout:
                    continue
                except (OSError, EOFError):
                    break
        finally:
            for s in (src, dst):
                try:
                    s.close()
                except Exception:
                    pass

    def stop(self):
        """Shut down the tunnel."""
        self._shutdown_event.set()
        try:
            self._server_sock.close()
        except Exception:
            pass
        try:
            self._client.close()
        except Exception:
            pass


class TunnelManager:
    """
    Singleton manager for SSH tunnel connections.

    Uses reference counting to keep the tunnel alive while connections exist,
    and closes it when the last connection is closed.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        with TunnelManager._lock:
            if self._initialized:
                return
            self._tunnel = None
            self._ref_count = 0
            self._tunnel_lock = threading.Lock()
            self._initialized = True
            atexit.register(self._cleanup_on_exit)

    def _cleanup_on_exit(self):
        """Ensure tunnel is closed when program exits."""
        with self._tunnel_lock:
            if self._tunnel is not None:
                try:
                    self._tunnel.stop()
                    logger.debug("SSH tunnel closed on exit")
                except Exception as e:
                    logger.warning(f"Error closing SSH tunnel on exit: {e}")
                self._tunnel = None
                self._ref_count = 0

    def acquire(self) -> int:
        """
        Acquire a tunnel reference. Starts tunnel if not already running.

        Returns:
            The local port to connect to.
        """
        with self._tunnel_lock:
            if self._tunnel is None:
                self._start_tunnel()
            self._ref_count += 1
            return self._tunnel.local_bind_port

    def release(self):
        """Release a tunnel reference. Closes tunnel when ref count reaches zero."""
        with self._tunnel_lock:
            self._ref_count -= 1
            if self._ref_count <= 0:
                self._stop_tunnel()
                self._ref_count = 0

    def _start_tunnel(self):
        """Start the SSH tunnel."""
        ssh_host = os.getenv("SSH_HOST")
        ssh_port = int(os.getenv("SSH_PORT", "22"))
        ssh_user = os.getenv("SSH_USER")
        ssh_key_path = os.getenv("SSH_KEY_PATH", "~/.ssh/id_rsa")
        ssh_key_passphrase = os.getenv("SSH_KEY_PASSPHRASE")

        # Remote bind defaults to DB_HOST/DB_PORT
        remote_bind_host = os.getenv("SSH_REMOTE_BIND_HOST", os.getenv("DB_HOST", "localhost"))
        remote_bind_port = int(os.getenv("SSH_REMOTE_BIND_PORT", os.getenv("DB_PORT", "3306")))

        # Expand ~ in key path
        ssh_key_path = str(Path(ssh_key_path).expanduser())

        logger.info(f"Starting SSH tunnel to {ssh_host}:{ssh_port} as {ssh_user}")
        logger.debug(f"Remote bind: {remote_bind_host}:{remote_bind_port}")

        self._tunnel = _ParamikoTunnel(
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_key_path=ssh_key_path,
            remote_host=remote_bind_host,
            remote_port=remote_bind_port,
            ssh_key_passphrase=ssh_key_passphrase or None,
        )

        logger.info(f"SSH tunnel established on local port {self._tunnel.local_bind_port}")

    def _stop_tunnel(self):
        """Stop the SSH tunnel."""
        if self._tunnel is not None:
            self._tunnel.stop()
            logger.info("SSH tunnel closed")
            self._tunnel = None


def _is_ssh_enabled() -> bool:
    """Check if SSH tunneling is enabled."""
    return os.getenv("SSH_ENABLED", "false").lower() in ("true", "1", "yes")


def cleanup_tunnel():
    """Explicitly cleanup the SSH tunnel. Called automatically on exit."""
    if _is_ssh_enabled():
        TunnelManager()._cleanup_on_exit()


def get_connection() -> mysql.connector.MySQLConnection:
    """
    Create a database connection using environment variables.

    If SSH_ENABLED is true, routes the connection through an SSH tunnel.
    Otherwise, connects directly to the database.
    """
    if _is_ssh_enabled():
        return _get_tunneled_connection()
    else:
        return _get_direct_connection()


def _get_direct_connection() -> mysql.connector.MySQLConnection:
    """Create a direct database connection."""
    return mysql.connector.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME"),
        user=os.getenv("DB_USER"),
        password=os.getenv("DB_PASSWORD"),
    )


def _get_tunneled_connection() -> mysql.connector.MySQLConnection:
    """Create a database connection through SSH tunnel."""
    tunnel_manager = TunnelManager()
    local_port = tunnel_manager.acquire()

    try:
        conn = mysql.connector.connect(
            host="127.0.0.1",
            port=local_port,
            database=os.getenv("DB_NAME"),
            user=os.getenv("DB_USER"),
            password=os.getenv("DB_PASSWORD"),
        )
    except Exception:
        # Release tunnel reference if connection fails
        tunnel_manager.release()
        raise

    # Wrap the close method to release tunnel reference
    original_close = conn.close
    _released = False
    _close_lock = threading.Lock()

    def close_with_tunnel_release():
        nonlocal _released
        with _close_lock:
            if _released:
                return
            _released = True
        original_close()
        tunnel_manager.release()

    conn.close = close_with_tunnel_release
    return conn


def fetch_items(offer_id: int, limit: int | None = None) -> list[OfferPart]:
    """
    Fetch F&V items for a given offer.

    Args:
        offer_id: The offer_id to filter by
        limit: Optional limit for testing (None = no limit)

    Returns:
        List of OfferPart objects
    """
    query = FETCH_ITEMS
    params: tuple = (offer_id,)

    if limit:
        query += " LIMIT %s"
        params = params + (limit,)

    items = []

    with closing(get_connection()) as conn:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute(query, params)
            rows = cursor.fetchall()

            for row in rows:
                items.append(OfferPart(
                    id=row[COL_ID],
                    name=row[COL_NAME],
                    price=row[COL_PRICE],
                    category_id=row[COL_CATEGORY],
                ))

    logger.info(f"Fetched {len(items)} F&V items for offer_id={offer_id}")
    return items


def execute_updates(updates: list[tuple[int, int]], dry_run: bool = False) -> int:
    """
    Execute RRP updates on the database.

    Args:
        updates: List of (rrp_cents, item_id) tuples
        dry_run: If True, don't actually execute the updates

    Returns:
        Number of rows updated
    """
    if not updates:
        return 0

    if dry_run:
        logger.info(f"[DRY RUN] Would update {len(updates)} items")
        return 0

    with closing(get_connection()) as conn:
        try:
            with conn.cursor() as cursor:
                cursor.executemany(UPDATE_RRP, updates)
                affected = cursor.rowcount
            conn.commit()
            logger.info(f"Updated {affected} items in database")
            return affected
        except Exception as e:
            conn.rollback()
            logger.error(f"Failed to execute updates: {e}")
            raise


def verify_offer_exists(offer_id: int) -> bool:
    """Check if an offer exists and has F&V items."""
    with closing(get_connection()) as conn:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute(VERIFY_OFFER, (offer_id,))
            row = cursor.fetchone()
            return row[COL_COUNT] > 0


def fetch_recent_offers(limit: int = 10) -> list[OfferInfo]:
    """
    Fetch recent offers with F&V item counts.

    Returns offers ordered by offer_id descending (most recent first).
    """
    offers = []

    with closing(get_connection()) as conn:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute(FETCH_RECENT_OFFERS, (limit,))
            rows = cursor.fetchall()

            for row in rows:
                offers.append(
                    OfferInfo(
                        offer_id=row[COL_OFFER],
                        item_count=row[COL_COUNT],
                        latest_updated=row[COL_LATEST],
                    )
                )

    logger.info(f"Fetched {len(offers)} recent offers")
    return offers


def fetch_current_rrp(item_ids: list[int]) -> dict[int, int | None]:
    """
    Fetch current RRP values for items.

    Returns:
        Dict mapping item_id to current RRP in cents (or None if not set).
    """
    if not item_ids:
        return {}

    # Build parameterized query
    placeholders = ", ".join(["%s"] * len(item_ids))
    query = FETCH_CURRENT_RRP.format(placeholders=placeholders)

    result = {}

    with closing(get_connection()) as conn:
        with conn.cursor(dictionary=True) as cursor:
            cursor.execute(query, tuple(item_ids))
            rows = cursor.fetchall()

            for row in rows:
                result[row[COL_ID]] = row[COL_RRP]

    return result
