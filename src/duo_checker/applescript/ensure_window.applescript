-- 引数: 既存ウィンドウ ID（なければ 0）
-- 既存ウィンドウが残っていればその ID を、なければ最小化した新しいウィンドウを作って ID を返す。
-- ページの読み込みは navigate.applescript で行う。
on run argv
	set winId to (item 1 of argv) as integer
	tell application "Safari"
		if winId is not 0 then
			-- 閉じたウィンドウはタブ 0 枚・非表示のまま一覧に残ることがあるため、タブの有無も確かめる
			if (exists window id winId) then
				if (count of tabs of window id winId) > 0 then return (winId as text) & "|existing"
			end if
		end if
		set beforeIds to {}
		repeat with w in windows
			try
				set end of beforeIds to (id of w)
			end try
		end repeat
		make new document with properties {URL:"about:blank"}
		repeat 40 times
			repeat with w in windows
				try
					set wid to id of w
					if beforeIds does not contain wid then
						try
							set miniaturized of w to true
						end try
						return (wid as text) & "|created"
					end if
				end try
			end repeat
			delay 0.25
		end repeat
	end tell
	error "Safari のウィンドウを作成できませんでした"
end run
