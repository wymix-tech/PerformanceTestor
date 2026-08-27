package com.perfserver.stress;

/**
 * 压测周期采样回调：引擎每完成一个采样周期调用一次，供实时图表落库。
 */
public interface StressSampleListener {

    void onSample(StressTaskResult.Sample sample);
}
