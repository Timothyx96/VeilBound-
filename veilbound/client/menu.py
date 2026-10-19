"""Main-menu flow: local hot-seat, host a room, or join a room.

Also drives the in-game pause menu (Options / Quit) and the audio settings
screen (SFX / music volume). Network handshakes run on the connection worker
thread, so the menu polls thread-safe events and never blocks the Pygame frame loop.
"""

from __future__ import annotations

import pygame

from veilbound.client.network import NetworkConnection, RemoteGameEngine
from veilbound.client.settings import DEFAULT_PRESETS
from veilbound.shared.engine import GameEngine
from veilbound.client.theme import BG, GOLD, PANEL, PANEL_LIGHT

CODE_LENGTH = 6
_EDITABLE_FIELDS = ("host", "port", "code")

# In-game animation speeds (offline modes only); order defines the option row.
_ANIMATION_PRESETS = ("Slow", "Normal", "Fast")

# Volume step per button press (0-100 scale).
_VOLUME_STEP = 10


def normalize_room_code(value: str) -> str:
    """Uppercase alphanumeric room code, trimmed to the server format."""
    return "".join(char for char in value.upper() if char.isalnum())[:CODE_LENGTH]


class MenuScreen:
    """Clickable title screen that yields a local or online game outcome.

    Also renders the pause (Options/Quit) and audio (volume) overlays used
    during a match. When ``screen`` is ``pause`` or ``audio`` no outcome is
    produced; the caller reads ``action`` instead.
    """

    def __init__(
        self,
        default_host: str = "127.0.0.1",
        default_port: int = 8765,
        music_volume: float = 0.35,
        sfx_volume: float = 0.8,
        animation_preset: str = "Slow",
    ):
        self.default_host = default_host
        self.default_port = default_port
        self.screen = "root"
        self.host = default_host
        self.port = str(default_port)
        self.code = ""
        self.active_field = "code"
        self.connection: NetworkConnection | None = None
        self.engine: RemoteGameEngine | None = None
        self.mode: str | None = None
        self.status = "Two players share the board. Play locally or over the network."
        self.error: str | None = None
        self.mouse_pos: tuple[int, int] | None = None
        self._outcome: tuple | None = None
        self._buttons: dict[str, pygame.Rect] = {}
        self.music_volume = min(1.0, max(0.0, float(music_volume)))
        self.sfx_volume = min(1.0, max(0.0, float(sfx_volume)))
        self.action: str | None = None
        # Live animation speed shown in the options screen (offline modes). The
        # app seeds this from the renderer when a match starts; clicks set
        # ``action = "speed:<Preset>"`` and the app applies it to the renderer.
        self.animation_preset = (
            animation_preset if animation_preset in _ANIMATION_PRESETS else "Slow"
        )
        # Set by the app when a match starts ("local"/"debug"/online); the pause
        # menu only offers a return to the main menu for the offline modes.
        self.game_mode: str | None = None

    def take_outcome(self) -> tuple | None:
        outcome, self._outcome = self._outcome, None
        return outcome

    def layout(self, size: tuple[int, int]) -> dict[str, pygame.Rect]:
        """Button geometry for the active screen; pure function of size."""
        width, height = size
        center = width // 2
        buttons: dict[str, pygame.Rect] = {}
        if self.screen == "root":
            top = height // 2 - 70
            for index, name in enumerate(("local", "debug", "host", "join")):
                buttons[name] = pygame.Rect(center - 230, top + index * 78, 460, 62)
            buttons["quit"] = pygame.Rect(center - 230, top + 4 * 78, 460, 62)
        elif self.screen == "join":
            buttons["connect"] = pygame.Rect(center - 230, height // 2 + 130, 220, 58)
            buttons["back"] = pygame.Rect(center + 10, height // 2 + 130, 220, 58)
        elif self.screen == "waiting":
            buttons["cancel"] = pygame.Rect(center - 150, height // 2 + 150, 300, 58)
        elif self.screen == "pause":
            if self.game_mode in {"local", "debug"}:
                # Offline modes also offer a direct return to the main menu.
                buttons["options"] = pygame.Rect(center - 150, height // 2 - 80, 300, 58)
                buttons["menu"] = pygame.Rect(center - 150, height // 2 - 10, 300, 58)
                buttons["quit"] = pygame.Rect(center - 150, height // 2 + 60, 300, 58)
            else:
                buttons["options"] = pygame.Rect(center - 150, height // 2 - 10, 300, 58)
                buttons["quit"] = pygame.Rect(center - 150, height // 2 + 70, 300, 58)
        elif self.screen == "audio":
            buttons["sfx_down"] = pygame.Rect(center - 340, height // 2 - 40, 56, 44)
            buttons["sfx_up"] = pygame.Rect(center + 284, height // 2 - 40, 56, 44)
            buttons["music_down"] = pygame.Rect(center - 340, height // 2 + 20, 56, 44)
            buttons["music_up"] = pygame.Rect(center + 284, height // 2 + 20, 56, 44)
            if self.game_mode in {"local", "debug"}:
                # Game Speed: one button per preset; the renderer already reads
                # animation_ms live, so applying is instant (see app.run_game).
                slot = 168
                total = slot * len(_ANIMATION_PRESETS) + 12 * (len(_ANIMATION_PRESETS) - 1)
                left = center - total // 2
                for index, preset in enumerate(_ANIMATION_PRESETS):
                    buttons[f"speed_{preset.lower()}"] = pygame.Rect(
                        left + index * (slot + 12), height // 2 + 80, slot, 44
                    )
                buttons["back"] = pygame.Rect(center - 150, height // 2 + 140, 300, 58)
            else:
                buttons["back"] = pygame.Rect(center - 150, height // 2 + 100, 300, 58)
        self._buttons = buttons
        return buttons

    def button_at(self, pos: tuple[int, int], size: tuple[int, int]) -> str | None:
        for name, rect in self.layout(size).items():
            if rect.collidepoint(pos):
                return name
        return None

    def fields(self) -> dict[str, str]:
        return {"host": self.host, "port": self.port, "code": self.code}

    # ------------------------------------------------------------------ input
    def handle(self, event: pygame.event.Event, screen: pygame.Surface) -> None:
        if self._outcome is not None:
            return
        if event.type == pygame.MOUSEMOTION:
            self.mouse_pos = event.pos
        elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1:
            self._click(event.pos, screen.get_size())
        elif event.type == pygame.KEYDOWN:
            self._key(event)

    def _click(self, pos: tuple[int, int], size: tuple[int, int]) -> None:
        self.mouse_pos = pos
        name = self.button_at(pos, size)
        if name is None:
            return
        if self.screen == "root":
            if name == "local":
                self._outcome = ("local",)
            elif name == "debug":
                self.start_debug()
            elif name == "host":
                self._start_host()
            elif name == "join":
                self.screen = "join"
                self.error = None
            elif name == "quit":
                self._outcome = ("quit",)
        elif self.screen == "join":
            if name == "connect":
                self._start_join()
            elif name == "back":
                self.screen = "root"
                self.error = None
        elif self.screen == "waiting" and name == "cancel":
            self._cancel_connection()
            self.screen = "root"
            self.status = "Connection cancelled."
        elif self.screen == "pause":
            if name == "options":
                self.screen = "audio"
                self.action = None
            elif name == "menu":
                self.action = "menu"
            elif name == "quit":
                self.action = "quit"
        elif self.screen == "audio":
            if name == "sfx_down":
                self.sfx_volume = max(0.0, self.sfx_volume - _VOLUME_STEP / 100.0)
            elif name == "sfx_up":
                self.sfx_volume = min(1.0, self.sfx_volume + _VOLUME_STEP / 100.0)
            elif name == "music_down":
                self.music_volume = max(0.0, self.music_volume - _VOLUME_STEP / 100.0)
            elif name == "music_up":
                self.music_volume = min(1.0, self.music_volume + _VOLUME_STEP / 100.0)
            elif name.startswith("speed_"):
                preset = name[len("speed_"):].capitalize()
                if preset in _ANIMATION_PRESETS:
                    # Handled by the game loop like resume/menu/quit: it applies
                    # the preset to the renderer and clears the action.
                    self.animation_preset = preset
                    self.action = f"speed:{preset}"
            elif name == "back":
                self.screen = "pause"
                self.action = None

    def _key(self, event: pygame.event.Event) -> None:
        if event.key == pygame.K_ESCAPE:
            if self.screen == "waiting":
                self._cancel_connection()
                self.screen = "root"
                self.status = "Connection cancelled."
            elif self.screen == "join":
                self.screen = "root"
                self.error = None
            elif self.screen == "audio":
                self.screen = "pause"
                self.action = None
            elif self.screen == "pause":
                self.action = "resume"
            # On the root menu Esc no longer quits; it simply does nothing.
            return
        if self.screen == "root":
            if event.key == pygame.K_1:
                self._outcome = ("local",)
            elif event.key == pygame.K_2:
                self.start_debug()
            elif event.key == pygame.K_3:
                self._start_host()
            elif event.key == pygame.K_4:
                self.screen = "join"
                self.error = None
            return
        if self.screen != "join":
            return
        if event.key == pygame.K_TAB:
            fields = list(_EDITABLE_FIELDS)
            step = -1 if event.mod & pygame.KMOD_SHIFT else 1
            self.active_field = fields[(fields.index(self.active_field) + step) % len(fields)]
        elif event.key == pygame.K_BACKSPACE:
            self._edit_active(lambda value: value[:-1])
        elif event.key in (pygame.K_RETURN, pygame.K_KP_ENTER):
            self._start_join()
        elif event.unicode:
            self._type_character(event.unicode)

    def _edit_active(self, transform) -> None:
        if self.active_field == "host":
            self.host = transform(self.host)
        elif self.active_field == "port":
            self.port = "".join(char for char in transform(self.port) if char.isdigit())
        else:
            self.code = normalize_room_code(transform(self.code))

    def _type_character(self, char: str) -> None:
        if self.active_field == "host" and len(self.host) < 64 and (char.isalnum() or char in ".-"):
            self.host += char
        elif self.active_field == "port" and char.isdigit() and len(self.port) < 5:
            self.port += char
        elif self.active_field == "code" and char.isalnum():
            self.code = normalize_room_code(self.code + char)

    # -------------------------------------------------------------- networking
    def _port_number(self) -> int:
        try:
            port = int(self.port)
        except ValueError:
            return 0
        return port if 1 <= port <= 65535 else 0

    def _start_host(self) -> None:
        port = self._port_number() or self.default_port
        self._open_connection("create", None, port)

    def _start_join(self) -> None:
        code = normalize_room_code(self.code)
        self.code = code
        if len(code) != CODE_LENGTH:
            self.error = f"Room codes have {CODE_LENGTH} characters."
            return
        port = self._port_number()
        if not port:
            self.error = "Enter a valid port (1-65535)."
            return
        self._open_connection("join", code, port)

    def start_debug(self) -> None:
        """Switch to Debug Mode: local 50-card roster game with scrollable hands."""
        self._cancel_connection()
        self.mode = "debug"
        self.engine = GameEngine(debug_mode=True)
        self.screen = "root"
        self.status = "Debug Mode: scroll the 50-card strip to select a card."
        self._outcome = ("debug", None, self.engine)

    def _open_connection(self, mode: str, code: str | None, port: int) -> None:
        self._cancel_connection()
        self.mode = mode
        self.error = None
        self.engine = None
        self.connection = NetworkConnection(self.host or self.default_host, port, mode, code)
        self.screen = "waiting"
        target = "room" if mode == "create" else f"room {code}"
        self.status = f"Connecting to {target} ..."

    def _cancel_connection(self) -> None:
        connection, self.connection = self.connection, None
        self.engine = None
        self.mode = None
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass

    def _fail(self, message: str) -> None:
        self._cancel_connection()
        self.screen = "root"
        self.error = message
        self.status = "Two players share the board. Play locally or over the network."

    def update(self) -> None:
        """Poll the worker thread; never blocks the frame loop."""
        connection = self.connection
        if connection is None or self._outcome is not None:
            return
        if connection.failed.is_set():
            self._fail(self._drain_error(connection) or "Could not reach the server.")
            return
        if not connection.connected.is_set():
            return
        if self.engine is None:
            self.engine = RemoteGameEngine(connection)
        self.engine.poll()
        notice = self.engine.drain_notice()
        if notice:
            self.status = notice
        if self.engine.state is not None and not self.engine.remote_locked:
            self._outcome = ("online", connection, self.engine)
            self.connection = None

    def _drain_error(self, connection: NetworkConnection) -> str | None:
        errors = []
        while True:
            try:
                message = connection.incoming.get_nowait()
            except Exception:
                break
            if isinstance(message, dict) and message.get("type") == "error":
                errors.append(str(message.get("message", "Connection failed.")))
        return errors[-1] if errors else None

    def waiting_lines(self) -> tuple[str, str]:
        code = None
        if self.engine is not None and self.engine.room_code:
            code = self.engine.room_code
        elif self.connection is not None and self.connection.room_code:
            code = self.connection.room_code
        if self.mode == "create":
            headline = f"Room {code}" if code else "Creating room ..."
            detail = "Share the code. Waiting for Player 2 to join."
        else:
            headline = f"Joining room {self.code} ..."
            detail = "Waiting for the host's game to start."
        return headline, detail

    # ------------------------------------------------------------------ drawing
    def draw(self, screen: pygame.Surface, renderer) -> None:
        screen.fill(BG)
        overlay_screen = self.screen in {"pause", "audio"}
        if overlay_screen and renderer.options_background is not None:
            screen.blit(
                renderer.options_background,
                renderer.options_background.get_rect(center=screen.get_rect().center),
            )
        else:
            # Main-menu flow (root/join/waiting): dedicated cover-fit art, no tint.
            renderer._draw_menu_background(screen)
        self.layout(screen.get_size())
        if self.screen == "root":
            self._draw_root(screen)
        elif self.screen == "join":
            self._draw_join(screen)
        elif self.screen == "waiting":
            self._draw_waiting(screen)
        elif self.screen == "pause":
            self._draw_pause(screen)
        elif self.screen == "audio":
            self._draw_audio(screen)

    def _draw_root(self, screen: pygame.Surface) -> None:
        labels = {
            "local": ("1 - Local Game", "Both players share this screen"),
            "debug": ("2 - Debug Mode", "All 50 cards, scrollable hand"),
            "host": ("3 - Host Online Room", "Get a code and let a friend join"),
            "join": ("4 - Join Online Room", "Enter a room code from the host"),
            "quit": ("Quit", ""),
        }
        for name, rect in self._buttons.items():
            headline, detail = labels[name]
            self._draw_button(screen, rect, headline, detail)

    def _draw_join(self, screen: pygame.Surface) -> None:
        from veilbound.client.typography import render_text

        width = screen.get_width()
        panel = pygame.Rect(width // 2 - 300, screen.get_height() // 2 - 120, 600, 230)
        pygame.draw.rect(screen, PANEL, panel, border_radius=14)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=14)
        heading = render_text("JOIN AN ONLINE ROOM", "ui", panel.width - 40)
        screen.blit(heading, heading.get_rect(center=(panel.centerx, panel.y + 30)))
        rows = (
            ("host", "Host", self.host, 240),
            ("port", "Port", self.port, 120),
            ("code", "Code", self.code or "______", 200),
        )
        for index, (field, label, value, _box_width) in enumerate(rows):
            y = panel.y + 66 + index * 38
            tag = render_text(label, "label_small", 120)
            screen.blit(tag, (panel.x + 30, y))
            active = self.active_field == field
            color = GOLD if active else PANEL_LIGHT
            box = pygame.Rect(panel.x + 130, y - 6, panel.width - 160, 30)
            pygame.draw.rect(screen, PANEL_LIGHT, box, border_radius=7)
            pygame.draw.rect(screen, color, box, width=2, border_radius=7)
            text = render_text(str(value), "ui", box.width - 16)
            screen.blit(text, (box.x + 10, box.y + 3))
        hint = render_text("Tab switches field - Enter connects - Esc goes back", "label_small", panel.width - 40)
        screen.blit(hint, hint.get_rect(center=(panel.centerx, panel.bottom - 20)))
        self._draw_button(screen, self._buttons["connect"], "Connect", "")
        self._draw_button(screen, self._buttons["back"], "Back", "")

    def _draw_waiting(self, screen: pygame.Surface) -> None:
        from veilbound.client.typography import render_text

        width, height = screen.get_size()
        headline, detail = self.waiting_lines()
        panel = pygame.Rect(width // 2 - 320, height // 2 - 120, 640, 260)
        pygame.draw.rect(screen, PANEL, panel, border_radius=16)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=16)
        big = render_text(headline, "title", panel.width - 60)
        screen.blit(big, big.get_rect(center=(panel.centerx, panel.y + 80)))
        small = render_text(detail, "ui", panel.width - 60)
        screen.blit(small, small.get_rect(center=(panel.centerx, panel.y + 140)))
        state_text = render_text(self.status, "label_small", panel.width - 60)
        screen.blit(state_text, state_text.get_rect(center=(panel.centerx, panel.y + 185)))
        self._draw_button(screen, self._buttons["cancel"], "Cancel (Esc)", "")

    def _draw_pause(self, screen: pygame.Surface) -> None:
        width, height = screen.get_size()
        has_menu = self.game_mode in {"local", "debug"}
        top_button = self._buttons["options"]
        bottom_button = self._buttons["quit"]
        # The panel wraps every button, so it adapts to the
        # offline (Options / Main Menu / Quit) and online (Options / Quit) sets.
        panel_top = top_button.y - 20
        panel = pygame.Rect(
            width // 2 - 220,
            panel_top,
            440,
            (bottom_button.bottom + 20) - panel_top,
        )
        pygame.draw.rect(screen, PANEL, panel, border_radius=16)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=16)
        self._draw_button(screen, self._buttons["options"], "Options", "")
        if has_menu:
            self._draw_button(screen, self._buttons["menu"], "Main Menu", "")
        self._draw_button(screen, self._buttons["quit"], "Quit Game", "")

    def _draw_audio(self, screen: pygame.Surface) -> None:
        from veilbound.client.typography import render_text

        width, height = screen.get_size()
        panel = pygame.Rect(width // 2 - 300, height // 2 - 110, 600, 260)
        pygame.draw.rect(screen, PANEL, panel, border_radius=16)
        pygame.draw.rect(screen, GOLD, panel, width=2, border_radius=16)

        def volume_label(volume: float) -> str:
            return f"{int(round(volume * 100))}%"

        # SFX row
        sfx_label = render_text("SFX Volume", "ui", 120)
        screen.blit(sfx_label, (panel.x + 40, panel.y + 80))
        sfx_value = render_text(volume_label(self.sfx_volume), "ui", 80)
        screen.blit(sfx_value, sfx_value.get_rect(center=(panel.centerx, panel.y + 102)))
        self._draw_button(screen, self._buttons["sfx_down"], "-", "")
        self._draw_button(screen, self._buttons["sfx_up"], "+", "")

        # Music row
        music_label = render_text("Music Volume", "ui", 120)
        screen.blit(music_label, (panel.x + 40, panel.y + 140))
        music_value = render_text(volume_label(self.music_volume), "ui", 80)
        screen.blit(music_value, music_value.get_rect(center=(panel.centerx, panel.y + 162)))
        self._draw_button(screen, self._buttons["music_down"], "-", "")
        self._draw_button(screen, self._buttons["music_up"], "+", "")

        if self.game_mode in {"local", "debug"}:
            # Game Speed row: one button per preset; the active preset renders
            # highlighted so the current speed reads at a glance.
            speed_label = render_text("Game Speed", "ui", 160)
            screen.blit(speed_label, (panel.x + 40, panel.y + 200))
            for preset in _ANIMATION_PRESETS:
                key = f"speed_{preset.lower()}"
                if key in self._buttons:
                    self._draw_button(
                        screen,
                        self._buttons[key],
                        preset,
                        "",
                        selected=(preset == self.animation_preset),
                    )

        self._draw_button(screen, self._buttons["back"], "Back", "")

    def _draw_button(self, screen: pygame.Surface, rect: pygame.Rect, headline: str, detail: str, selected: bool = False) -> None:
        from veilbound.client.typography import render_text

        hovered = self.mouse_pos is not None and rect.collidepoint(self.mouse_pos)
        highlighted = hovered or selected
        pygame.draw.rect(screen, PANEL_LIGHT if highlighted else PANEL, rect, border_radius=10)
        pygame.draw.rect(screen, GOLD, rect, width=3 if highlighted else 2, border_radius=10)
        head = render_text(headline, "ui", rect.width - 32)
        screen.blit(head, head.get_rect(center=(rect.centerx, rect.centery - (8 if detail else 0))))
        if detail:
            sub = render_text(detail, "label_small", rect.width - 32)
            screen.blit(sub, sub.get_rect(center=(rect.centerx, rect.centery + 18)))

