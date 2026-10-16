-- 引数: ウィンドウ ID, 実行する JavaScript, 結果をポーリングする JavaScript, タイムアウト秒
-- 非同期処理（fetch）を開始し、結果が window に格納されるまで待って返す。
on run argv
	set winId to (item 1 of argv) as integer
	set startJs to item 2 of argv
	set pollJs to item 3 of argv
	set timeoutSec to (item 4 of argv) as real
	tell application "Safari"
		set t to current tab of window id winId
		do JavaScript startJs in t
		set waited to 0
		repeat while waited < timeoutSec
			delay 0.25
			set waited to waited + 0.25
			set res to do JavaScript pollJs in t
			if res is not missing value and res is not "" then return res
		end repeat
	end tell
	return "__TIMEOUT__"
end run
