from __future__ import annotations

import argparse

import pygame

from veilbound.client.network import NetworkConnection, RemoteGameEngine
from veilbound.shared.engine import GameEngine
from .assets import AssetLoader
from .audio import AudioManager
from .input import InputController
from .menu import MenuScreen
from .renderer import Renderer
from .settings import load_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Play Veilbound locally or online")
    parser.add_argument("--host", default="127.0.0.1", help="Multiplayer server address")
    parser.add_argument("--port", type=int, default=8765, help="Multiplayer server port")
    return parser.parse_args(argv)


def toggle_display(event: pygame.event.Event, screen: pygame.Surface, windowed_size: tuple[int, int], fullscreen: bool):
    """Shared fullscreen/resize handling for the menu and game loops."""
    toggle_fullscreen = event.type == pygame.KEYDOWN and (
        event.key == pygame.K_F11
        or (event.key == pygame.K_RETURN and event.mod & pygame.KMOD_ALT)
    )
    if toggle_fullscreen:
        if fullscreen:
            screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE, depth=32)
        else:
            windowed_size = screen.get_size()
            screen = pygame.display.set_mode((0, 0), pygame.FULLSCREEN, depth=32)
        fullscreen = not fullscreen
    elif event.type == pygame.VIDEORESIZE and not fullscreen:
        size = (max(640, event.w), max(660, event.h))
        screen = pygame.display.set_mode(size, pygame.RESIZABLE, depth=32)
        windowed_size = size
    else:
        return None
    return screen, windowed_size, fullscreen


def main(argv: list[str] | None = None) -> None:
    options = parse_args(argv)
    pygame.init()
    pygame.display.set_caption("Veilbound")
    windowed_size = (1280, 900)
    screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE, depth=32)
    fullscreen = False
    clock = pygame.time.Clock()
    settings = load_settings()
    assets = AssetLoader()
    renderer = Renderer(assets, settings)
    audio = AudioManager(settings.audio, assets.root)

    running = True
    while running:
        menu = MenuScreen(options.host, options.port)
        outcome = run_menu(menu, renderer, clock)
        if outcome is None or outcome[0] == "quit":
            break
        if outcome[0] == "local":
            engine: GameEngine | RemoteGameEngine = GameEngine()
            connection: NetworkConnection | None = None
            renderer.room_label = None
        else:
            _, connection, engine = outcome
            renderer.room_label = f"Room {engine.room_code or connection.room_code or 'online'}"
        controller = InputController(engine, renderer, audio)
        running = run_game(engine, controller, renderer, clock)
        if connection is not None:
            connection.close()
        renderer.room_label = None
        renderer.reset_animations()
        renderer.notice = "Drag a card into a valid empty slot."
        audio.reset_match()
    pygame.quit()


def run_menu(menu: MenuScreen, renderer: Renderer, clock: pygame.time.Clock) -> tuple | None:
    """Drive the menu until it yields an outcome or the window closes."""
    windowed_size = (1280, 900)
    screen = pygame.display.get_surface()
    fullscreen = False
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None
            display = toggle_display(event, screen, windowed_size, fullscreen)
            if display is not None:
                screen, windowed_size, fullscreen = display
            else:
                menu.handle(event, screen)
        menu.update()
        screen = pygame.display.get_surface() or screen
        menu.draw(screen, renderer)
        pygame.display.flip()
        outcome = menu.take_outcome()
        if outcome is not None:
            return outcome
        clock.tick(30)


def run_game(engine, controller: InputController, renderer: Renderer, clock: pygame.time.Clock) -> bool:
    """Run one local or online match; returns False when the app should quit."""
    windowed_size = (1280, 900)
    screen = pygame.display.get_surface()
    fullscreen = False
    running = True
    while running:
        for event in pygame.event.get():
            display = toggle_display(event, screen, windowed_size, fullscreen)
            if display is not None:
                screen, windowed_size, fullscreen = display
            else:
                running = controller.handle(event, screen) and running
        controller.update()
        # set_mode() can replace SDL's display surface during resize/fullscreen
        # changes. Always draw to the current surface, not a stale reference.
        screen = pygame.display.get_surface() or screen
        renderer.draw(
            screen,
            engine.state,
            engine.cards,
            engine.legal_slots(),
            controller.drag_id,
            controller.mouse_pos,
            fps=clock.get_fps(),
        )
        pygame.display.flip()
        clock.tick(0)
    return False


if __name__ == "__main__":
    main()

