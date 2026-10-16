from __future__ import annotations

import argparse

import pygame

from veilbound.client.network import NetworkConnection, RemoteGameEngine
from veilbound.client.intro import VideoIntro, run_intro
from veilbound.shared.engine import GameEngine
from .assets import AssetLoader
from .audio import AudioManager, MUSIC_GAME, MUSIC_LOADING, MUSIC_LOBBY
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
        menu = MenuScreen(options.host, options.port, settings.music_volume, settings.sfx_volume)
        outcome = run_menu(menu, renderer, clock, audio)
        if outcome is None or outcome[0] == "quit":
            break
        # A menu outcome means the match has entered its loading state.
        audio.play_music(MUSIC_LOADING)
        if outcome[0] == "local":
            engine: GameEngine | RemoteGameEngine = GameEngine()
            connection: NetworkConnection | None = None
            renderer.room_label = None
        else:
            _, connection, engine = outcome
            renderer.room_label = f"Room {engine.room_code or connection.room_code or 'online'}"
        # Play pre-match intro video before entering the board. The video has
        # no audio track, so loading music continues underneath it, then the
        # match's game music starts when gameplay actually begins.
        intro = VideoIntro(assets.root, screen.get_size())
        if not run_intro(intro, screen, clock):
            break
        audio.play_music(MUSIC_GAME)
        controller = InputController(engine, renderer, audio)
        running = run_game(engine, controller, renderer, clock, menu, audio)
        if connection is not None:
            connection.close()
        renderer.room_label = None
        renderer.reset_animations()
        renderer.notice = "Drag a card into a valid empty slot."
        audio.reset_match()
    # Normal shutdown: play the exit track in full, then quit.
    audio.play_exit_music()
    pygame.quit()


def run_menu(menu: MenuScreen, renderer: Renderer, clock: pygame.time.Clock, audio: AudioManager) -> tuple | None:
    """Drive the menu until it yields an outcome or the window closes.

    The startup/lobby track plays continuously across every lobby screen; the
    same-key guard in ``play_music`` keeps it from restarting while the lobby
    updates or the player navigates between screens.
    """
    windowed_size = (1280, 900)
    screen = pygame.display.get_surface()
    fullscreen = False
    # Startup and the main lobby share one track; a no-op when already playing.
    audio.play_music(MUSIC_LOBBY)
    active_screen = menu.screen
    while True:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                return None
            display = toggle_display(event, screen, windowed_size, fullscreen)
            if display is not None:
                screen, windowed_size, fullscreen = display
            else:
                menu.handle(event, screen)
        if menu.screen == "audio":
            audio.enter_options()
        elif active_screen == "audio":
            audio.leave_options()
        active_screen = menu.screen
        menu.update()
        screen = pygame.display.get_surface() or screen
        menu.draw(screen, renderer)
        pygame.display.flip()
        outcome = menu.take_outcome()
        if outcome is not None:
            return outcome
        clock.tick(30)


def run_game(engine, controller: InputController, renderer: Renderer, clock: pygame.time.Clock, menu: MenuScreen, audio: AudioManager) -> bool:
    """Run one local or online match; returns False when the app should quit."""
    windowed_size = (1280, 900)
    screen = pygame.display.get_surface()
    fullscreen = False
    running = True
    paused = False
    while running:
        for event in pygame.event.get():
            display = toggle_display(event, screen, windowed_size, fullscreen)
            if display is not None:
                screen, windowed_size, fullscreen = display
            elif paused:
                menu.handle(event, screen)
                if menu.action == "resume":
                    paused = False
                    menu.action = None
                elif menu.action == "quit":
                    running = False
                elif menu.action is None and event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                    # Esc while in pause audio/submenu: handled by menu; resume only from pause root.
                    pass
            else:
                result = controller.handle(event, screen)
                if result == "pause":
                    paused = True
                    menu.screen = "pause"
                    menu.action = None
                elif result is False:
                    running = False
                else:
                    running = result and running
        # ESC opened the in-game options/pause menu: fade game music into the
        # options track; closing it (returning to gameplay) resumes game music.
        if paused:
            audio.enter_options()
        else:
            audio.leave_options()
        if paused:
            # Apply live volume changes from the audio screen.
            audio.set_music_volume(menu.music_volume)
            audio.set_sfx_volume(menu.sfx_volume)
            controller.update()
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
            menu.draw(screen, renderer)
            pygame.display.flip()
            clock.tick(30)
            continue
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

