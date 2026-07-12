ALTER TABLE public.users
ADD COLUMN IF NOT EXISTS username TEXT DEFAULT 'User';

UPDATE public.users
SET username = 'User-' || LEFT(id::text, 8)
WHERE username IS NULL OR BTRIM(username) = '';

ALTER TABLE public.users
ALTER COLUMN username SET NOT NULL;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'users_username_not_blank'
      AND conrelid = 'public.users'::regclass
  ) THEN
    ALTER TABLE public.users
    ADD CONSTRAINT users_username_not_blank
    CHECK (BTRIM(username) <> '') NOT VALID;
  END IF;
END
$$;

ALTER TABLE public.users
VALIDATE CONSTRAINT users_username_not_blank;
