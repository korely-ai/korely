-- Korely Installer.app (29 Oct 2026): the download for a Mac, for whoever
-- would rather not open a terminal. Compiled to Resources/Scripts/main.scpt
-- and run by the app's executable (main.swift). It asks three things in native dialogs
-- (Docker, the folder, optionally the model provider and its key), then opens
-- Terminal running install.sh, the exact copy of this release bundled in the
-- app (scripts/make_mac_installer.sh checks it), so that every step is seen.
-- No installer logic lives here or in launcher.sh: install.sh decides.
--
-- The key is typed in a hidden-answer dialog and written by AppleScript
-- itself into a file in a folder of the user's alone (mktemp -d), which
-- launcher.sh reads and deletes: it is never part of a `do shell script`
-- command, of the command Terminal runs, or of anybody's argv.
--
-- KORELY_INSTALLER_FROM_LOCAL, KORELY_INSTALLER_PORT and
-- KORELY_INSTALLER_PROJECT are for tests only (install.sh's --from-local,
-- --port and --project): nobody sets them, and a user never sees them.

property appTitle : "Korely Installer"
property dockerHelp : "https://docs.docker.com/desktop/setup/install/mac-install/"
property dockerPath : "PATH=\"/usr/local/bin:/opt/homebrew/bin:$HOME/.docker/bin:/Applications/Docker.app/Contents/Resources/bin:/usr/bin:/bin\"; export PATH; "
property skipLabel : "Skip for now"
property providerLabels : {"Skip for now", "Gemini (Google)", "Scaleway (EU)", "Mistral (EU)", "OpenAI", "OpenRouter"}
property providerIds : {"", "gemini", "scaleway", "mistral", "openai", "openrouter"}

on run
	set appIcon to (POSIX file (resourcePath("AppIcon.icns"))) as alias
	waitForDocker(appIcon)
	set installDir to askForTheFolder(appIcon)
	set providerChoice to askForTheProvider(appIcon)
	set providerId to item 1 of providerChoice
	set theKey to ""
	if providerId is not "" then
		set theKey to askForTheKey(item 2 of providerChoice, appIcon)
		if theKey is "" then set providerId to ""
	end if
	startTheInstall(installDir, providerId, theKey)
	set theKey to ""
	display dialog "Korely is installing in the Terminal window that just opened. The first time takes a few minutes." & return & return & "At the end your dashboard opens in the browser, and Terminal shows your login and password: copy them, they are shown once." buttons {"OK"} default button "OK" with title appTitle with icon appIcon giving up after 120
end run

-- A file of the app's Resources. `path to resource` would look in the
-- script's own bundle, and main.scpt has none: the app is the host.
on resourcePath(fileName)
	return (POSIX path of (path to current application)) & "Contents/Resources/" & fileName
end resourcePath

-- Docker Desktop, installed and answering, or the user is told what to do.
on dockerAnswers()
	try
		do shell script dockerPath & "docker info >/dev/null 2>&1"
		return true
	on error
		return false
	end try
end dockerAnswers

on dockerInstalled()
	try
		do shell script dockerPath & "command -v docker >/dev/null 2>&1 || test -d /Applications/Docker.app || test -d \"$HOME/Applications/Docker.app\" || test -n \"$(/usr/bin/mdfind \"kMDItemCFBundleIdentifier == 'com.docker.docker'\" 2>/dev/null | head -n 1)\""
		return true
	on error
		return false
	end try
end dockerInstalled

on waitForDocker(appIcon)
	repeat until dockerAnswers()
		if dockerInstalled() then
			display dialog "Korely runs in Docker Desktop, which is installed on this Mac but not running." & return & return & "Start it now? This installer waits for it, up to two minutes." buttons {"Quit", "Start Docker"} default button "Start Docker" cancel button "Quit" with title appTitle with icon appIcon
			try
				do shell script "open -b com.docker.docker || open -a Docker"
			end try
			-- Docker Desktop shows its own window while it starts.
			repeat 60 times
				if dockerAnswers() then exit repeat
				delay 2
			end repeat
			if not dockerAnswers() then
				display dialog "Docker Desktop is still starting." & return & return & "If it shows a window asking something (its terms, a sign-in you can skip), answer it, then click Try again." buttons {"Quit", "Try again"} default button "Try again" cancel button "Quit" with title appTitle with icon appIcon
			end if
		else
			set r to display dialog "Korely runs in Docker Desktop, which is not on this Mac yet. It is free for personal use and for small businesses." & return & return & "Get Docker Desktop, install it and start it, then come back here and click Try again." buttons {"Quit", "Try again", "Get Docker Desktop"} default button "Get Docker Desktop" cancel button "Quit" with title appTitle with icon appIcon
			if button returned of r is "Get Docker Desktop" then
				open location dockerHelp
				display dialog "When Docker Desktop is installed and running, click Try again." buttons {"Quit", "Try again"} default button "Try again" cancel button "Quit" with title appTitle with icon appIcon
			end if
		end if
	end repeat
end waitForDocker

-- The folder: ~/Korely unless the user picks another place for it.
on askForTheFolder(appIcon)
	set homePath to POSIX path of (path to home folder)
	set installDir to homePath & "Korely"
	-- A ~/Korely that is not an install (another Korely app may keep its
	-- data there) is never offered: install.sh would refuse it anyway.
	try
		do shell script "test -e " & quoted form of installDir & " && ! test -f " & quoted form of (installDir & "/.korely-install")
		set installDir to homePath & "Korely Agents"
	end try
	repeat
		set shown to installDir
		if shown starts with homePath then set shown to "~/" & (text ((length of homePath) + 1) thru -1 of shown)
		set r to display dialog "Korely Agents will be installed in this folder:" & return & return & shown & return & return & "It runs on this Mac, in Docker. Nothing outside this folder changes." buttons {"Quit", "Change...", "Install here"} default button "Install here" cancel button "Quit" with title appTitle with icon appIcon
		if button returned of r is "Change..." then
			try
				set parentFolder to choose folder with prompt "Choose where to put the Korely folder:" default location (path to home folder)
				set installDir to (POSIX path of parentFolder) & "Korely"
			end try
		else
			if installDir contains linefeed or installDir contains return then
				display dialog "That folder's name has a line break in it. Choose another place." buttons {"OK"} default button "OK" with title appTitle with icon appIcon
			else
				return installDir
			end if
		end if
	end repeat
end askForTheFolder

-- The model provider, optionally: {id, label}, id "" for none.
on askForTheProvider(appIcon)
	set picked to choose from list providerLabels with title appTitle with prompt "Korely uses an AI model, with your own API key, to turn what your agents remember into facts." & return & return & "Which provider is your key from? Not sure: skip, and the dashboard asks later." default items {skipLabel} OK button name "Next" cancel button name "Quit"
	if picked is false then error number -128
	set pickedLabel to item 1 of picked
	repeat with i from 1 to count of providerLabels
		if item i of providerLabels is pickedLabel then return {item i of providerIds, pickedLabel}
	end repeat
	return {"", skipLabel}
end askForTheProvider

-- The key, in a hidden-answer field, trimmed of the spaces a paste brings.
on askForTheKey(providerLabel, appIcon)
	repeat
		set r to display dialog "Paste your " & providerLabel & " API key." & return & return & "It is kept only in the settings file of your Korely folder, and nothing calls the provider during the install." default answer "" with hidden answer buttons {"Quit", "Skip", "Continue"} default button "Continue" cancel button "Quit" with title appTitle with icon appIcon
		if button returned of r is "Skip" then return ""
		set k to my trimmed(text returned of r)
		if k is not "" then return k
	end repeat
end askForTheKey

on trimmed(s)
	set blanks to {space, tab, return, linefeed}
	repeat while s is not "" and (character 1 of s) is in blanks
		if length of s is 1 then return ""
		set s to text 2 thru -1 of s
	end repeat
	repeat while s is not "" and (character -1 of s) is in blanks
		if length of s is 1 then return ""
		set s to text 1 thru -2 of s
	end repeat
	return s
end trimmed

-- The answers into a file of the user's alone, then Terminal.
on startTheInstall(installDir, providerId, theKey)
	set launcher to resourcePath("launcher.sh")
	set tmp to do shell script "t=${TMPDIR:-/tmp}; /usr/bin/mktemp -d \"${t%/}/korely-installer.XXXXXX\""
	set answers to installDir & linefeed & providerId & linefeed & theKey & linefeed & (system attribute "KORELY_INSTALLER_FROM_LOCAL") & linefeed & (system attribute "KORELY_INSTALLER_PORT") & linefeed & (system attribute "KORELY_INSTALLER_PROJECT") & linefeed
	set fh to open for access (POSIX file (tmp & "/answers")) with write permission
	try
		set eof fh to 0
		write answers to fh as «class utf8»
		close access fh
	on error errText number errNumber
		close access fh
		error errText number errNumber
	end try
	set answers to ""
	do shell script "/bin/sh " & quoted form of launcher & " terminal " & quoted form of tmp
end startTheInstall
