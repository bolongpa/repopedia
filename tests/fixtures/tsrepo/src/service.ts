import { helper } from './util';

export class Base {
  name(): string {
    return 'base';
  }
}

export class Service extends Base {
  async run(task: string): Promise<string> {
    const cleaned = helper(task);
    this.log(cleaned);
    return cleaned;
  }

  private log(msg: string): void {
    console.log(msg);
  }
}

export function launch(): void {
  const svc = new Service('x');
  svc.run('hello');
}
