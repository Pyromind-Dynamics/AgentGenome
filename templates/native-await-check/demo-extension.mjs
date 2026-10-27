// Optional test tool: load explicitly alongside the AgentGenome extension.
import {spawn} from 'node:child_process';
import {mkdir} from 'node:fs/promises';
import {isAbsolute, join} from 'node:path';
import {randomUUID} from 'node:crypto';

export default function (pi) {
  pi.registerTool({
    name: 'demo_async_submit', label: 'Async await demo',
    description: 'Start a real local background process; returns its receipt before completion. Test directories only.',
    parameters: {type: 'object', properties: {output_dir: {type: 'string'}}, required: ['output_dir'], additionalProperties: false},
    async execute(_id, args, signal) {
      if (!isAbsolute(args.output_dir)) throw new Error('output_dir must be absolute');
      signal?.throwIfAborted();
      await mkdir(args.output_dir); // Refuse to overwrite an existing test directory.
      const receipt = {task_id: randomUUID(), output_dir: args.output_dir};
      const producer = `const fs=require('node:fs'); const p=require('node:path');
        const dir=process.argv[1];
        fs.appendFileSync(p.join(dir,'submit-count.txt'),'x');
        setTimeout(()=>{
          fs.writeFileSync(p.join(dir,'processed.jsonl'),'{"actual":true}\\n');
          fs.writeFileSync(p.join(dir,'report.json'),JSON.stringify({status:'succeeded'}));
        },4000);`;
      const child = spawn(process.execPath, ['-e', producer, args.output_dir], {detached: true, stdio: 'ignore'});
      await new Promise((resolve, reject) => {child.once('spawn', resolve); child.once('error', reject);});
      child.unref();
      return {content: [{type: 'text', text: JSON.stringify(receipt)}], details: receipt};
    },
  });
}
