-- Keeps the free Render server awake: Supabase calls /health every 10 minutes.
-- Undo with: select cron.unschedule('keep-render-awake');
create extension if not exists pg_cron;
create extension if not exists pg_net;

select cron.unschedule('keep-render-awake') where exists (select 1 from cron.job where jobname = 'keep-render-awake');
select cron.schedule('keep-render-awake', '*/10 * * * *',
                     $$select net.http_get('https://second-brain-xbgr.onrender.com/health')$$);
