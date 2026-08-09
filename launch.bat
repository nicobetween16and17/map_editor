@echo off
SETLOCAL EnableExtensions EnableDelayedExpansion

echo ===================================================
echo   Verification de l'environnement de l'application
echo ===================================================

:: 1. Verifier si Python est installe sur le systeme
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo Python n'est pas detecte. Telechargement de l'installateur officiel...
    winget install -e --id cURL.cURL
    curl -L -o python_installer.exe https://python.org

    echo Installation silencieuse de Python en cours, merci de patienter...
    python_installer.exe /quiet InstallAllUsers=0 PrependPath=1 Include_test=0
    del python_installer.exe

    :: Force Windows a rafraichir ses variables d'environnement
    set "PATH=%USERPROFILE%\AppData\Local\Programs\Python\Python311\;%USERPROFILE%\AppData\Local\Programs\Python\Python311\Scripts\;%PATH%"

    echo Python a ete installe avec succes !
)

:: 2. Creer l'environnement virtuel local s'il n'existe pas
if not exist ".venv" (
    echo Creation de l'environnement virtuel venv...
    python -m venv .venv
)

:: 3. Activer le venv et verifier les dependences
echo Verification et mise a jour des dependances...
call .venv\Scripts\activate.bat

if exist "requirements.txt" (
    python -m pip install --upgrade pip -q
    python -m pip install -r requirements.txt -q
) else (
    python -m pip install PyQt5 -q
)

:: 4. Lancer l'application
echo Lancement de l'application...
start .venv\Scripts\pythonw.exe script.py

exit
