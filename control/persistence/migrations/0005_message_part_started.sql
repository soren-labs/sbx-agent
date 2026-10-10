-- When a MessagePart first appeared. With updated_at it bounds how long a streamed
-- reply, a reasoning block or a tool call actually took. Parts written before this
-- migration keep NULL: their start was never recorded and is not invented.
ALTER TABLE message_parts ADD COLUMN created_at timestamptz;
ALTER TABLE message_parts ALTER COLUMN created_at SET DEFAULT now();
