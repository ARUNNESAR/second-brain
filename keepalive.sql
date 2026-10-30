-- Keeps the free Render server awake: Supabase calls /health every 10 minutes, 6:30 AM - 1:30 AM India time.
-- (cron is in UTC: hours 1-19 UTC = 06:30-01:20 IST.) It sleeps ~5 h a night so it uses ~590 of Render's 750 free
-- hours a month, leaving room for another free service. A message at night still wakes it (~1 minute).
-- Undo with: select cron.unschedule('keep-render-awake');
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.unschedule('keep-render-awake') where exists (select 1 from cron.job where jobname = 'keep-render-awake');
select cron.schedule('keep-render-awake', '*/10 1-19 * * *',
                     $$select net.http_get('https://second-brain-xbgr.onrender.com/health')$$);
