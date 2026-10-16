-- 引数: ウィンドウ ID, URL, 読み込み完了とみなす URL の接頭辞, タイムアウト秒
-- 指定ウィンドウのタブで URL を開き、新しいページの DOM が読み込まれるまで待つ。
-- 最小化したウィンドウでは画像などの読み込みが終わらず readyState が complete にならないことがあるため、
-- interactive（DOM 構築済み）でも完了とみなす。古いページと区別するため、遷移前に印を付けておく。
on run argv
	set winId to (item 1 of argv) as integer
	set targetUrl to item 2 of argv
	set prefix to item 3 of argv
	set timeoutSec to (item 4 of argv) as real
	tell application "Safari"
		set t to current tab of window id winId
		try
			do JavaScript "window.__duoOldPage = true" in t
		end try
		set URL of t to targetUrl
		delay 1
		set waited to 0
		repeat while waited < timeoutSec
			try
				set rs to do JavaScript "(window.__duoOldPage ? 'old' : document.readyState) + '|' + location.href" in t
				if (rs starts with ("interactive|" & prefix)) or (rs starts with ("complete|" & prefix)) then return rs
			end try
			delay 0.5
			set waited to waited + 0.5
		end repeat
	end tell
	return "__TIMEOUT__"
end run
