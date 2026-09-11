"""Shared public client catalog. No Telegram transport dependencies."""
DOWNLOAD_CATALOG = {
    "ios": (
        {
            "id": "karing",
            "label_key": "download_karing_recommended",
            "url": "https://apps.apple.com/us/app/karing/id6472431552",
            "details_key": "download_karing_ios_tutorial",
        },
        {
            "id": "happ",
            "label_key": "download_happ",
            "url": "https://apps.apple.com/us/app/happ-proxy-utility/id6504287215",
            "details_key": "download_happ_ios_details",
        },
    ),
    "android": (
        {
            "id": "v2ray",
            "label": "v2rayNG",
            "url": "https://github.com/2dust/v2rayNG/releases/latest",
            "details_key": "download_v2rayng_android_details",
        },
    ),
    "windows": (
        {
            "id": "v2ray",
            "label": "v2rayN",
            "url": "https://github.com/2dust/v2rayN/releases/latest",
            "details_key": "download_v2rayn_windows_details",
        },
    ),
}

PLATFORM_LABELS = {
    "ios": "📱 iOS",
    "android": "📱 Android",
    "windows": "💻 Windows",
}

