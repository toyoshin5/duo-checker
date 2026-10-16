-- 引数: タイトル, 本文
on run argv
	display notification (item 2 of argv) with title (item 1 of argv) sound name "Glass"
end run
