ALTER TABLE public.wordbook_words
ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now();

CREATE OR REPLACE FUNCTION public.set_wordbook_words_updated_at()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS wordbook_words_set_updated_at ON public.wordbook_words;
CREATE TRIGGER wordbook_words_set_updated_at
BEFORE UPDATE ON public.wordbook_words
FOR EACH ROW
EXECUTE FUNCTION public.set_wordbook_words_updated_at();

CREATE INDEX IF NOT EXISTS wordbook_words_user_learned_updated_idx
ON public.wordbook_words(user_id, is_learned, updated_at DESC);

ALTER TABLE public.users
ADD COLUMN IF NOT EXISTS last_opened_material_id UUID;

DO $$
BEGIN
  IF NOT EXISTS (
    SELECT 1
    FROM pg_constraint
    WHERE conname = 'users_last_opened_material_id_fkey'
      AND conrelid = 'public.users'::regclass
  ) THEN
    ALTER TABLE public.users
    ADD CONSTRAINT users_last_opened_material_id_fkey
    FOREIGN KEY (last_opened_material_id)
    REFERENCES public.materials(id)
    ON DELETE SET NULL;
  END IF;
END
$$;
