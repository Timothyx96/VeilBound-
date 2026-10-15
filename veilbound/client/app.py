from __future__ import annotations

import pygame

from veilbound.shared.engine import GameEngine
from .assets import AssetLoader
from .audio import AudioManager
from .input import InputController
from .renderer import Renderer
from .settings import load_settings


def main() -> None:
    pygame.init()
    pygame.display.set_caption("Veilbound")
    windowed_size = (1280, 900)
    screen = pygame.display.set_mode(windowed_size, pygame.RESIZABLE, depth=32)
    fullscreen = False
    clock = pygame.time.Clock()
    settings = load_settings()
    assets = AssetLoader()
    engine = GameEngine()
    renderer = Renderer(assets, settings)
    audio = AudioManager(settings.audio, assets.root)
    controller = InputController(engine, renderer, audio)
    running = True
    while running:
        for event in pygame.event.get():
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
    pygame.quit()


if __name__ == "__main__":
    main()

