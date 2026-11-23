// TrendLens Genie matrix test (quick mode, offline): node run.js <genie.js> <webdir>
const fs=require('fs'),path=require('path');const W=process.argv[3]||path.join(__dirname,'..');const G=require(process.argv[2]||path.join(W,'api','genie.js'));
const H=JSON.parse(fs.readFileSync(W+'/history.json')),F=JSON.parse(fs.readFileSync(W+'/fundamentals.json')),P=JSON.parse(fs.readFileSync(W+'/picks.json'));
global.fetch=async()=>{throw new Error('offline')};delete process.env.ANTHROPIC_API_KEY;
const nm=s=>(F.stocks[s]&&F.stocks[s].name)||s;
const short=s=>nm(s).replace(/\b(limited|ltd|ltd\.|corporation|company|industries|india|of|the)\b/gi,'').replace(/\s+/g,' ').trim();
const T=[];const add=(q,exp)=>T.push({q,exp});
const BAD=['unknown','out_of_scope'];
// one or two words
['market','nifty','help','momentum','ideas','top stocks','gainers','losers','top gainers','top losers','52 week high','breakout','oversold','overbought','dividend','high dividend','pharma','banks','it','it stocks','auto','fmcg','metal','metals','cement','power','realty','chemicals','telecom','insurance','nbfc','defence','psu','psu banks','infra','energy','oil','small cap','midcap','large cap','cheap stocks','undervalued','low pe','high roe','debt free','penny stocks','best stocks','sectors','news','rsi','macd','pe','roe','beta','support','what is momentum','live ideas','closed ideas','track record','win rate','vix','sensex','bank nifty','today','stocks','buy','portfolio']
 .forEach(w=>add(w,{notBad:true}));
const U=H.universe;const pick=U.filter((s,i)=>i%Math.ceil(U.length/30)===0).slice(0,30);
pick.forEach(s=>{add(s,{sym:s});add(s.toLowerCase(),{sym:s});add(short(s),{sym:s})});
const TOP=[['{s} price','price'],['{s} share price today','price'],['{s} pe','fund'],['what is the pe of {s}','fund'],['{s} roe','fund'],['{s} debt','fund'],['{s} market cap','fund'],['{s} dividend','fund'],['{s} fundamentals','fund'],['{s} rsi','tech'],['{s} outlook','tech'],['{s} support','tech'],['{s} trend','tech'],['{s} 52 week high','avg'],['{s} returns','avg'],['{s} news','news'],['why is {s} down','why'],['why is {s} up today','why'],['{s} momentum rank','mom'],['is {s} a good buy',null],['{s}?',null],['tell me about {s}',null],['{s} target','idea'],['how is {s} doing',null],['{s} chart','tech'],['{s} 1 year return','avg']];
pick.slice(0,20).forEach(s=>TOP.forEach(([tp,top])=>add(tp.replace('{s}',short(s)),{sym:s,topic:top})));
for(let i=0;i<10;i++){const a=pick[i],b=pick[(i*7+5)%pick.length];if(a===b)continue;['{a} vs {b}','{a} or {b}','compare {a} and {b}','which is better {a} or {b}','{a} versus {b}'].forEach(tp=>add(tp.replace('{a}',short(a)).replace('{b}',short(b)),{kind:'compare'}))}
['pharma stocks with roe above 15','banks with pe below 15','top 10 momentum stocks','stocks near 52 week high','high dividend stocks','low debt it stocks','best performing stocks this year','top gainers today','it stocks with pe under 25','debt free stocks','stocks with rsi below 30','cheapest bank stocks','metal stocks near 52 week low','auto stocks','fmcg stocks with high roe'].forEach(q=>add(q,{kind:'screen'}));
['what is rsi','rsi meaning','what is macd','what is pe ratio','explain momentum','what is beta','what is 52 week high'].forEach(q=>add(q,{kind:'glossary'}));
[['relaince','RELIANCE'],['infosis','INFY'],['tata motor',null],['hdfc bnk','HDFCBANK'],['bajaj finanace','BAJFINANCE'],['tcs share','TCS'],['reliance ka share price','RELIANCE'],['infy kitna hai','INFY'],['itc news','ITC'],['sbi rsi','SBIN']].forEach(([q,s])=>add(q,s?{sym:s}:{notBad:true}));
(async()=>{const out=[];for(const t of T){let r;try{r=await G._test.answer(t.q,{},H,F,P)}catch(e){r={kind:'ERR',text:e.stack}}
 const why=[];const txt=String(r.text||'');
 if(r.kind==='ERR')why.push('error');if(BAD.includes(r.kind))why.push('kind='+r.kind);if(r.kind==='ambiguous'&&t.exp.sym)why.push('ambiguous');
 if(t.exp.sym&&r.sym!==t.exp.sym&&!(r.syms||[]).includes(t.exp.sym))why.push('sym='+r.sym+' want '+t.exp.sym);
 if(t.exp.topic&&!(r.topics||[]).includes(t.exp.topic))why.push('topics='+JSON.stringify(r.topics)+' want '+t.exp.topic);
 if(t.exp.kind&&r.kind!==t.exp.kind)why.push('kind='+r.kind+' want '+t.exp.kind);
 if(!txt.trim())why.push('empty');
 out.push({q:t.q,ok:!why.length,why,kind:r.kind,a:txt.replace(/\s+/g,' ').slice(0,200)})}
 const bad=out.filter(o=>!o.ok);console.log('TOTAL',out.length,'FAILED',bad.length);bad.slice(0,20).forEach(o=>console.log(' -',o.q,'::',o.why.join('; ')));process.exitCode=bad.length?1:0;
 fs.writeFileSync(require('os').tmpdir()+'/genie_fail.txt',bad.map(o=>`${o.q}\n   ${o.why.join('; ')} [${o.kind}] ${o.a}`).join('\n'));
 fs.writeFileSync(require('os').tmpdir()+'/genie_all.txt',out.map(o=>`${o.ok?'OK':'XX'} ${o.q}\n   [${o.kind}] ${o.a}`).join('\n'))})();
