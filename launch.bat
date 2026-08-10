@echo off
SETLOCAL EnableExtensions EnableDelayedExpansion

echo ===================================================
echo   Verification de l'environnement de l'application
echo ===================================================

:: 1. Verifier si Python est installe sur le systeme
python --version >nul 2>&1

if errorlevel 1 (
    echo Python n'est pas detecte.
    echo Installation de Python 64 bits...

    winget install --id Python.Python.3.13 -e --source winget --accept-source-agreements --accept-package-agreements
    if errorlevel 1 (
        echo.
        echo ERREUR : impossible d'installer Python automatiquement.
        echo Installe Python 64 bits depuis python.org puis relance ce fichier.
        pause
        exit /b 1
    )
    :: Recharger le PATH
    set "PATH=%LOCALAPPDATA%\Programs\Python\Python313;%LOCALAPPDATA%\Programs\Python\Python313\Scripts;%PATH%"
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
