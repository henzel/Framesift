"""Message catalogs for engine strings (categories, reasons, warnings). No Qt here."""

from __future__ import annotations

import locale
import os
from typing import Any

MESSAGES: dict[str, dict[str, str]] = {
    "en": {
        "category.duplicates": "Duplicates",
        "category.screenshots": "Screenshots",
        "category.screen_recordings": "Screen recordings",
        "category.short_videos": "Short videos",
        "category.no_camera_media": "Media without camera data",
        "category.junk": "Junk",
        "category.blurry_dark": "Blurry or dark",
        "category.similar": "Similar shots",
        "category.live_photos": "Live Photo videos",
        "category.edited_pairs": "Original + edited pairs",
        "category.large_videos": "Largest videos",
        "confidence.high": "high",
        "confidence.medium": "medium",
        "confidence.low": "low",
        "reason.duplicate_of": "Exact copy of {keeper}",
        "reason.screenshot_comment": 'EXIF UserComment says "Screenshot"',
        "reason.screenshot_resolution": "PNG without camera data at a phone screen size ({width}×{height})",
        "reason.screen_recording_name": "File name looks like an iOS screen recording",
        "reason.screen_recording_resolution": "Video without camera data at a phone screen size ({width}×{height})",
        "reason.short_video": "Video is {seconds:.1f} s long (threshold {threshold:.0f} s)",
        "reason.no_camera": "No camera make or model in metadata",
        "reason.junk_name": "System or metadata file",
        "reason.junk_empty": "File is empty (0 bytes)",
        "reason.junk_broken": "File cannot be decoded ({detail})",
        "reason.junk_orphan_aae": ".AAE edit sidecar without its photo",
        "reason.junk_aae": ".AAE edit sidecar (option: move all)",
        "reason.blurry": "Very low sharpness ({sharpness:.1f} < {threshold:.1f})",
        "reason.dark": "Almost black frame (mean brightness {brightness:.0%})",
        "reason.similar_member": "Looks like {keeper} taken {delta:.0f} s apart",
        "reason.similar_keeper": "Best shot of group {group}",
        "reason.live_video": "Live Photo video of {photo}",
        "action.keep": "Keep",
        "action.to_delete": "To delete",
        "action.to_review": "Staged for review",
        "action.restore": "Restored",
        "action.detach_live": "Live Photo video moved",
        "action.purge": "Deleted",
        "action.undo": "Undo",
        "roots.apple_photos_library": "The source is inside an Apple Photos library ({detail}). Moving files there breaks the library, so Framesift refuses to work on it.",
        "roots.source_missing": "Source folder does not exist: {detail}",
        "roots.source_unreadable": "Source folder cannot be read: {detail}",
        "roots.lightroom_catalog": "A Lightroom catalog was found in the source. Moving files will make Lightroom lose track of them.",
        "roots.review_contains_source": "The Review folder must not contain the Source folder.",
        "roots.delete_contains_source": "The Delete folder must not contain the Source folder.",
        "roots.review_equals_delete": "Review and Delete folders must differ.",
        "roots.different_volumes": "The three folders are not on the same volume: moves will copy data and verify hashes, which takes time.",
        "roots.network_volume": "The source is on a network volume. The heavy analysis is faster when run on the NAS itself.",
        "purge.network_warning": "This volume has no system trash: files will be deleted permanently. On a NAS, the shared-folder recycle bin may still catch them, and snapshots may keep using the space.",
    },
    "ru": {
        "category.duplicates": "Дубликаты",
        "category.screenshots": "Скриншоты",
        "category.screen_recordings": "Записи экрана",
        "category.short_videos": "Короткие видео",
        "category.no_camera_media": "Без данных камеры",
        "category.junk": "Мусор",
        "category.blurry_dark": "Размытые или тёмные",
        "category.similar": "Похожие кадры",
        "category.live_photos": "Видео Live Photo",
        "category.edited_pairs": "Пары оригинал + правка",
        "category.large_videos": "Самые большие видео",
        "confidence.high": "высокая",
        "confidence.medium": "средняя",
        "confidence.low": "низкая",
        "reason.duplicate_of": "Точная копия {keeper}",
        "reason.screenshot_comment": "В EXIF UserComment записано «Screenshot»",
        "reason.screenshot_resolution": "PNG без данных камеры с разрешением экрана телефона ({width}×{height})",
        "reason.screen_recording_name": "Имя файла как у записи экрана iOS",
        "reason.screen_recording_resolution": "Видео без данных камеры с разрешением экрана телефона ({width}×{height})",
        "reason.short_video": "Длительность {seconds:.1f} с (порог {threshold:.0f} с)",
        "reason.no_camera": "В метаданных нет производителя и модели камеры",
        "reason.junk_name": "Системный или служебный файл",
        "reason.junk_empty": "Пустой файл (0 байт)",
        "reason.junk_broken": "Файл не декодируется ({detail})",
        "reason.junk_orphan_aae": "Файл правок .AAE без фотографии",
        "reason.junk_aae": "Файл правок .AAE (опция «все .AAE»)",
        "reason.blurry": "Очень низкая резкость ({sharpness:.1f} < {threshold:.1f})",
        "reason.dark": "Почти чёрный кадр (средняя яркость {brightness:.0%})",
        "reason.similar_member": "Похож на {keeper}, снят с разницей {delta:.0f} с",
        "reason.similar_keeper": "Лучший кадр группы {group}",
        "reason.live_video": "Видео Live Photo для {photo}",
        "action.keep": "Оставить",
        "action.to_delete": "На удаление",
        "action.to_review": "В ревью",
        "action.restore": "Возвращено",
        "action.detach_live": "Видео Live Photo перенесено",
        "action.purge": "Удалено",
        "action.undo": "Отмена",
        "roots.apple_photos_library": "Источник находится внутри библиотеки Apple Photos ({detail}). Перемещение файлов сломает библиотеку, поэтому Framesift отказывается с ней работать.",
        "roots.source_missing": "Папка источника не существует: {detail}",
        "roots.source_unreadable": "Папку источника не удалось прочитать: {detail}",
        "roots.lightroom_catalog": "В источнике найден каталог Lightroom. После перемещения файлов Lightroom потеряет их.",
        "roots.review_contains_source": "Папка Ревью не должна содержать папку источника.",
        "roots.delete_contains_source": "Папка К удалению не должна содержать папку источника.",
        "roots.review_equals_delete": "Папки Ревью и К удалению должны различаться.",
        "roots.different_volumes": "Три папки лежат на разных томах: перемещение будет копированием с проверкой хеша и займёт время.",
        "roots.network_volume": "Источник на сетевом томе. Тяжёлый анализ быстрее выполнять прямо на NAS.",
        "purge.network_warning": "На этом томе нет системной корзины: файлы будут удалены безвозвратно. На NAS их может подхватить корзина общей папки, а снапшоты могут удерживать место.",
    },
}

_lang_override: str | None = None


def set_language(lang: str | None) -> None:
    global _lang_override
    _lang_override = lang


def current_language() -> str:
    if _lang_override in MESSAGES:
        return _lang_override
    env = os.environ.get("FRAMESIFT_LANG")
    if env and env[:2] in MESSAGES:
        return env[:2]
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        value = os.environ.get(var)
        if value:
            code = value[:2].lower()
            return code if code in MESSAGES else "en"
    try:
        code = (locale.getlocale()[0] or "en")[:2].lower()
    except Exception:  # pragma: no cover
        code = "en"
    return code if code in MESSAGES else "en"


def t(key: str, lang: str | None = None, **params: Any) -> str:
    """Translate an engine message key. Unknown keys return the key itself."""
    lang = lang or current_language()
    template = MESSAGES.get(lang, {}).get(key) or MESSAGES["en"].get(key) or key
    try:
        return template.format(**params) if params else template
    except (KeyError, ValueError, IndexError):
        return template


def reason_text(
    reason_key: str, reason_params: dict[str, Any] | None, lang: str | None = None
) -> str:
    return t(reason_key, lang, **(reason_params or {}))
