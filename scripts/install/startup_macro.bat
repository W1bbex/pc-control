@echo off
if exist "%APPDATA%\Telegram Desktop\Telegram.exe" start "" "%APPDATA%\Telegram Desktop\Telegram.exe"
if exist "%LOCALAPPDATA%\Programs\YandexMusicMod\Яндекс Музыка.exe" start "" "%LOCALAPPDATA%\Programs\YandexMusicMod\Яндекс Музыка.exe"
if exist "%ProgramFiles%\Google\Chrome\Application\chrome.exe" start "" "%ProgramFiles%\Google\Chrome\Application\chrome.exe" --profile-directory="Default"
if exist "%LOCALAPPDATA%\Programs\antigravity\Antigravity.exe" start "" "%LOCALAPPDATA%\Programs\antigravity\Antigravity.exe"
if exist "%ProgramFiles%\FlyFrogLLC\Happ\Happ.exe" start "" "%ProgramFiles%\FlyFrogLLC\Happ\Happ.exe"
