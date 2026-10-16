-- 引数: ウィンドウ ID, JavaScript
on run argv
	set winId to (item 1 of argv) as integer
	tell application "Safari"
		set res to do JavaScript (item 2 of argv) in current tab of window id winId
	end tell
	if res is missing value then return ""
	return res
end run
